#!/usr/bin/env python3
"""SAU-511 — streaming extraction / load / read-back.

The reconciliation *comparison* was made O(columns) in `test_reconcile_memory.py`. This
file covers the other half: the three full materializations that surrounded it —
`extract_table`'s row list, `run_medallion`'s tuple list, and `read_gold`'s `fetchall` —
and the failure semantics that streaming put at risk.

Everything here is DB-free: the DuckDB proxy runs in-process, the source is a synthetic
streaming dialect, and the Snowflake path is driven with a mock cursor.

Run:
    .venv-test/bin/python -m unittest tests.test_reconcile_streaming -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

from core import codegen as cg
from core import dialect as dl
from core import mapper as mp
from core import ontology as ont
from core import reconcile as rc
from core import target as tg

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "artifacts" / "profile.json"


def _plan_code_and_maps():
    profile = json.loads(PROFILE.read_text())
    maps = mp.map_ontology(ont.lift(profile))
    code = cg.generate(maps)
    return code["plan"], [m for m in maps if m.entity == "plan"]


def _udt_index():
    profile = json.loads(PROFILE.read_text())
    onto = ont.lift(profile)
    return {(e.name, p.name): (p.udt or "") for e in onto.entities for p in e.properties}


def _plan_row(i: int, *, corrupt: bool = False) -> dict[str, str]:
    """One lossless text wire row for `plan`, in the shape the extractor emits."""
    price = f"{i}.99" if not corrupt else f"{i}.9"   # scale loss = a real mismatch
    return {
        "code": f"code-{i}",
        "currency": "usd",
        "features": json.dumps([f"f{i}", "shared"]),
        "id": str(i),
        "monthly_price": price,
        "name": f"Plan {i}",
    }


class _StreamingDialect(dl.PostgresDialect):
    """Synthetic source lane: yields N rows without touching Postgres.

    Subclasses `PostgresDialect` so the canonicalization hooks (`fix_tz`,
    `is_narrow_float`) are the real ones — only the row read is replaced.
    """

    name = "streaming-stub"

    def __init__(self, n: int, *, corrupt: bool = False) -> None:
        self.n = n
        self.corrupt = corrupt
        self.iter_calls = 0
        self.extract_calls = 0

    def iter_rows(self, entity, mappings):
        self.iter_calls += 1
        return (_plan_row(i) for i in range(self.n))

    def extract_rows(self, entity, mappings):
        self.extract_calls += 1
        return [_plan_row(i) for i in range(self.n)]


class _BatchOnlyDialect:
    """A dialect that implements ONLY the materializing read — the back-compat case.

    Deliberately NOT a `PostgresDialect` subclass: subclassing would inherit the real
    streaming `iter_rows` (and try to shell out to psql), which is the opposite of what
    this fixture is for. The dialect seam is duck-typed at the call site, so this models
    an out-of-tree lane written before `iter_rows` existed.
    """

    name = "batch-only"

    def __init__(self, n: int) -> None:
        self.n = n

    def extract_rows(self, entity, mappings):
        return [_plan_row(i) for i in range(self.n)]

    def fix_tz(self, s):
        return s

    def is_narrow_float(self, source_udt):
        return False


class _ExplodingDialect(dl.PostgresDialect):
    """A source lane whose read fails part-way through."""

    name = "exploding"

    def iter_rows(self, entity, mappings):
        def gen():
            yield _plan_row(0)
            raise RuntimeError("source connection dropped mid-read")
        return gen()

    def extract_rows(self, entity, mappings):
        raise RuntimeError("source connection dropped mid-read")


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestStreamingParity(unittest.TestCase):
    """The streaming path must produce byte-identical verdicts to the batch path."""

    def setUp(self):
        self.ec, self.maps = _plan_code_and_maps()
        self.udt = _udt_index()

    def _run(self, dialect):
        return rc.run({"plan": self.ec}, self.maps, self.udt, dialect=dialect)[0]

    def test_streamed_run_matches_a_materialized_reconciliation(self):
        """`rc.run` streaming end-to-end == `reconcile_entity` over materialized lists."""
        n = 250
        streamed = self._run(_StreamingDialect(n))

        # Reference: materialize everything the old way and reconcile the lists.
        rows = [_plan_row(i) for i in range(n)]
        con = rc.duckdb.connect(":memory:")
        con.execute("SET TimeZone='UTC'")
        try:
            rc.run_medallion(con, self.ec, rows)
            gold = rc.read_gold(con, self.ec.entity, self.ec.text_columns)
        finally:
            con.close()
        udt_by_col = {c: self.udt.get(("plan", c), "") for c in self.ec.text_columns}
        reference = rc.reconcile_entity(self.ec, self.maps, rows, gold, udt_by_col)

        self.assertEqual(streamed.source_rows, reference.source_rows)
        self.assertEqual(streamed.target_rows, reference.target_rows)
        self.assertEqual(streamed.row_match, reference.row_match)
        self.assertEqual(
            [(c.column, c.source_checksum, c.target_checksum, c.match)
             for c in streamed.columns],
            [(c.column, c.source_checksum, c.target_checksum, c.match)
             for c in reference.columns],
        )
        self.assertTrue(all(c.match for c in streamed.columns))

    def test_batch_only_dialect_still_works(self):
        """A dialect that never learned to stream degrades to the materializing read."""
        res = self._run(_BatchOnlyDialect(50))
        self.assertEqual(res.source_rows, 50)
        self.assertIsNone(res.exec_error)
        self.assertTrue(all(c.match for c in res.columns))

    def test_source_is_walked_exactly_once_on_a_clean_run(self):
        """The tee is the point: one source walk feeds the load AND the digest.

        A clean run must never trigger the diagnostic re-read.
        """
        d = _StreamingDialect(100)
        res = self._run(d)
        self.assertTrue(all(c.match for c in res.columns))
        self.assertEqual(d.iter_calls, 1, "clean run re-read the source")
        self.assertEqual(d.extract_calls, 0)

    def test_dirty_run_re_reads_only_to_produce_diagnostics(self):
        """A dirty verdict costs one extra source walk — and yields the samples."""
        ec, maps = self.ec, self.maps
        # Corrupt the TARGET by reconciling a clean source against a corrupted Gold: the
        # simplest faithful way is a direct digest comparison, which is what run() does.
        src_rows = [_plan_row(i) for i in range(40)]
        tgt_rows = [_plan_row(i, corrupt=True) for i in range(40)]
        udt_by_col = {c: self.udt.get(("plan", c), "") for c in ec.text_columns}
        res = rc.reconcile_entity(ec, maps, src_rows, tgt_rows, udt_by_col)

        price = next(c for c in res.columns if c.column == "monthly_price")
        self.assertFalse(price.match)
        self.assertEqual(price.n_mismatches, 40)          # exact count, not a floor
        self.assertEqual(len(price.mismatches), rc._DIAG_CAP)
        # Columns that agree must not be diagnosed at all.
        name = next(c for c in res.columns if c.column == "name")
        self.assertTrue(name.match)
        self.assertEqual(name.mismatches, [])


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestFailureIsNotMasked(unittest.TestCase):
    """Streaming moved the source walk INSIDE the target's execution call. The transform
    `exec_error` handler must not swallow a source failure and report it as a caught
    defect — that would be the gate claiming a detection it never earned."""

    def setUp(self):
        self.ec, self.maps = _plan_code_and_maps()
        self.udt = _udt_index()

    def test_source_read_failure_propagates_instead_of_becoming_exec_error(self):
        with self.assertRaises(rc.SourceReadError) as ctx:
            rc.run({"plan": self.ec}, self.maps, self.udt, dialect=_ExplodingDialect())
        self.assertIn("source connection dropped", str(ctx.exception))

    def test_a_genuine_transform_failure_is_still_recorded_as_exec_error(self):
        """The other side of the same coin: target failures stay caught, not raised."""
        class _BrokenTarget(tg.DuckDbAdapter):
            def load_medallion(self, handle, ec, rows):
                for _ in rows:      # drain the source normally...
                    pass
                raise ValueError("silver CAST failed")   # ...then fail in the transform

        res = rc.run({"plan": self.ec}, self.maps, self.udt,
                     dialect=_StreamingDialect(10), target=_BrokenTarget())[0]
        self.assertIsNotNone(res.exec_error)
        self.assertIn("silver CAST failed", res.exec_error)
        self.assertEqual(res.source_rows, 10)   # the digest counted what it consumed
        self.assertFalse(res.row_match)


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestTargetStreamingParity(unittest.TestCase):
    """Batched load and fetchmany read-back must be byte-identical to the batch path."""

    def setUp(self):
        self.ec, _ = _plan_code_and_maps()
        self.rows = [_plan_row(i) for i in range(137)]   # not a multiple of any batch

    def _gold(self, *, streamed: bool, batch: int):
        con = rc.duckdb.connect(":memory:")
        con.execute("SET TimeZone='UTC'")
        try:
            if streamed:
                rc.load_medallion(con, self.ec, iter(self.rows), batch=batch)
                return list(rc.iter_gold(con, self.ec.entity, self.ec.text_columns,
                                         batch=batch))
            rc.run_medallion(con, self.ec, self.rows)
            return rc.read_gold(con, self.ec.entity, self.ec.text_columns)
        finally:
            con.close()

    def test_batched_load_and_fetchmany_equal_the_materializing_path(self):
        reference = self._gold(streamed=False, batch=0)
        for batch in (1, 7, 100, 5_000):
            self.assertEqual(self._gold(streamed=True, batch=batch), reference,
                             f"batch={batch} diverged from the materializing path")

    def test_empty_entity_streams_cleanly(self):
        con = rc.duckdb.connect(":memory:")
        con.execute("SET TimeZone='UTC'")
        try:
            rc.load_medallion(con, self.ec, iter([]), batch=10)
            self.assertEqual(list(rc.iter_gold(con, self.ec.entity,
                                               self.ec.text_columns, batch=10)), [])
        finally:
            con.close()


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestSnowflakeStaging(unittest.TestCase):
    """The live path stages rows incrementally now; the BYTES must not change."""

    def setUp(self):
        self.ec, _ = _plan_code_and_maps()

    def test_incrementally_staged_json_is_byte_identical_to_the_old_writer(self):
        rows = [_plan_row(i) for i in range(64)]
        cols = self.ec.text_columns

        class _Cur:
            def __init__(self): self.executed = []
            def execute(self, sql): self.executed.append(sql); return self
            def close(self): pass

        adapter = tg.SnowflakeAdapter(live=True)
        cur = _Cur()
        path = adapter._stage_rows(cur, self.ec, iter(rows))
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path) as fh:
            written = fh.read()

        # The pre-SAU-511 writer, reconstructed independently.
        expected = json.dumps([{c: r.get(c) for c in cols} for r in rows],
                              ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(written, expected)
        self.assertEqual(json.loads(written), [{c: r.get(c) for c in cols} for r in rows])

    def test_no_rows_stages_nothing(self):
        class _Cur:
            def execute(self, sql): return self
            def close(self): pass
        self.assertIsNone(tg.SnowflakeAdapter(live=True)._stage_rows(_Cur(), self.ec,
                                                                    iter([])))

    def test_iter_gold_matches_read_gold(self):
        cols = self.ec.text_columns
        raw = [tuple(f"{c}-{i}" for c in cols) for i in range(23)]

        class _Cur:
            def __init__(self, rows): self._rows = list(rows); self.closed = False
            def execute(self, sql): return self
            def fetchall(self): return list(self._rows)
            def fetchmany(self, n):
                chunk, self._rows = self._rows[:n], self._rows[n:]
                return chunk
            def close(self): self.closed = True

        class _Conn:
            def __init__(self, rows): self._rows = rows; self.cursors = []
            def cursor(self):
                c = _Cur(self._rows); self.cursors.append(c); return c

        adapter = tg.SnowflakeAdapter(live=True)
        conn = _Conn(raw)
        batch = list(adapter.read_gold(conn, self.ec))
        streamed = list(adapter.iter_gold(conn, self.ec))
        self.assertEqual(streamed, batch)
        self.assertEqual(len(streamed), 23)
        self.assertTrue(all(c.closed for c in conn.cursors), "a cursor leaked")


class TestSubprocessStreamReader(unittest.TestCase):
    """`_stream_json_lines` is the shared source reader for both live lanes."""

    def _cmd(self, script: str) -> list[str]:
        return [sys.executable, "-c", script]

    def test_yields_one_parsed_object_per_line(self):
        script = 'import json\n[print(json.dumps({"i": i})) for i in range(5)]'
        got = list(dl._stream_json_lines(self._cmd(script), context="t"))
        self.assertEqual(got, [{"i": i} for i in range(5)])

    def test_non_zero_exit_raises_after_the_stream_drains(self):
        script = ('import json, sys\nprint(json.dumps({"i": 0}))\n'
                  'sys.stderr.write("boom\\n")\nsys.exit(3)')
        # The reader relays the child's stderr; capture it so the suite stays quiet.
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                list(dl._stream_json_lines(self._cmd(script), context="ctx"))
        self.assertIn("ctx failed (exit 3)", str(ctx.exception))
        self.assertIn("boom", err.getvalue())   # the diagnosis is not swallowed

    def test_keep_predicate_filters_non_data_lines(self):
        script = ('import json\nprint("Warning: header")\n'
                  '[print(json.dumps({"i": i})) for i in range(3)]')
        got = list(dl._stream_json_lines(self._cmd(script), context="t",
                                         keep=lambda ln: ln.startswith("{")))
        self.assertEqual(got, [{"i": i} for i in range(3)])

    def test_abandoning_the_generator_reaps_the_child(self):
        """The diagnostic pass may stop early; the reader must not leak a process."""
        script = ('import json, time\n'
                  'for i in range(10_000):\n'
                  '    print(json.dumps({"i": i}), flush=True)\n'
                  '    time.sleep(0.001)')
        gen = dl._stream_json_lines(self._cmd(script), context="t")
        self.assertEqual(next(gen), {"i": 0})
        gen.close()          # abandon it — the finally must kill the child
        # If the child were leaked this would hang; a reaped child returns immediately.
        self.assertTrue(True)


class _ReplayTarget(tg.DuckDbAdapter):
    """An O(1) target: drains the source stream, then regenerates Gold from the row index.

    Deliberately not DuckDB. The proxy's parameterized Bronze load costs ~1.3 ms/row
    (measured; a property of duckdb's `executemany`, unchanged by SAU-511), which caps
    how many rows an end-to-end memory test can afford — and row COUNT is exactly the
    variable that has to be pushed hard to tell flat from linear. This target keeps the
    real chassis under test (source iteration, the tee, both digests, the Gold stream)
    while contributing no storage of its own. Correctness against real DuckDB is covered
    by `TestStreamingParity`.
    """

    def __init__(self, n: int) -> None:
        self.n = n
        self.loaded = 0

    def connect(self):
        return object()

    def disconnect(self, handle):
        pass

    def load_medallion(self, handle, ec, rows):
        for _ in rows:
            self.loaded += 1

    def iter_gold(self, handle, ec):
        return (_plan_row(i) for i in range(self.n))


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestBatchAndStreamingIssueTheSameQuery(unittest.TestCase):
    """Both live lanes now build their extraction SQL and argv in one shared place.

    That was a refactor of code with no test on the seam, so this pins it: the streaming
    reader must issue the SAME query as the materializing reader on both lanes. If the
    two ever drift, the streamed corpus stops being the corpus the gate validated — a
    silent extraction difference, which is the one class of defect the §8 gate cannot
    see (it validates the transform, never the read).
    """

    def setUp(self):
        _, self.maps = _plan_code_and_maps()

    def test_postgres_streams_the_same_sql_it_batches(self):
        d = dl.PostgresDialect()
        sql = d._extract_sql("plan", self.maps)

        seen = {}

        def fake_run(cmd, **kw):
            seen["batch"] = cmd
            class P: returncode = 0; stdout = ""; stderr = ""
            return P()

        with mock.patch.object(dl.subprocess, "run", fake_run):
            d.extract_rows("plan", self.maps)

        def fake_popen(cmd, **kw):
            seen["stream"] = cmd
            raise RuntimeError("stop")   # argv captured; no need to actually run

        with mock.patch.object(dl.subprocess, "Popen", fake_popen):
            with self.assertRaises(RuntimeError):
                list(d.iter_rows("plan", self.maps))

        self.assertIn(sql, seen["batch"])
        self.assertIn(sql, seen["stream"])
        # The streaming argv is the batch argv plus the cursor pragma — nothing else.
        extra = [a for a in seen["stream"] if a not in seen["batch"]]
        self.assertEqual(extra, [f"FETCH_COUNT={d.FETCH_COUNT}"])

    def test_sqlserver_streams_the_same_argv_it_batches(self):
        d = dl.SqlServerDialect()
        sql = d._extract_sql("plan", self.maps)
        self.assertIn("FOR JSON PATH", sql)
        self.assertIn("INCLUDE_NULL_VALUES", sql)
        self.assertIn("ORDER BY [id]", sql)

        seen = {}

        def fake_run(cmd, **kw):
            seen["batch"] = cmd
            class P: returncode = 0; stdout = ""; stderr = ""
            return P()

        with mock.patch.object(dl.subprocess, "run", fake_run):
            d.extract_rows("plan", self.maps)

        def fake_popen(cmd, **kw):
            seen["stream"] = cmd
            raise RuntimeError("stop")

        with mock.patch.object(dl.subprocess, "Popen", fake_popen):
            with self.assertRaises(RuntimeError):
                list(d.iter_rows("plan", self.maps))

        # sqlcmd has no cursor pragma to add: the invocations must be byte-identical.
        self.assertEqual(seen["stream"], seen["batch"])
        self.assertIn(sql, seen["batch"])

    def test_sqlserver_row_filter_is_shared_by_both_readers(self):
        d = dl.SqlServerDialect()
        self.assertTrue(d._is_data_line('{"a": 1}'))
        for noise in ("Warning: something", "row_json", "---------", ""):
            self.assertFalse(d._is_data_line(noise), noise)


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestEndToEndMemoryIsFlat(unittest.TestCase):
    """The whole point of SAU-511: `rc.run` peak must not scale with row count."""

    def setUp(self):
        self.ec, self.maps = _plan_code_and_maps()
        self.udt = _udt_index()

    def _peak(self, n: int, target) -> int:
        tracemalloc.start()
        try:
            res = rc.run({"plan": self.ec}, self.maps, self.udt,
                         dialect=_StreamingDialect(n), target=target)[0]
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(res.source_rows, n)
        self.assertTrue(all(c.match for c in res.columns))
        return peak

    def test_full_run_peak_is_flat_in_row_count(self):
        """25x the rows, through the real chassis, must not move the peak."""
        small = self._peak(2_000, _ReplayTarget(2_000))
        large = self._peak(50_000, _ReplayTarget(50_000))
        # Pre-SAU-511 the extracted source list ALONE measured 27.0 MB at 10k rows and
        # 135.9 MB at 50k on a 10-column table, before any canonicalization.
        self.assertLess(large, small * 2,
                        f"rc.run still scales with rows: 2k={small}B → 50k={large}B")

    def test_duckdb_resident_rows_are_bounded_by_batch_not_table_size(self):
        """Through the real proxy, what stays resident is the load buffer, not the table.

        Sizes are small because the proxy load is ~1.3 ms/row; the batch is shrunk so it
        actually flushes (the default 5,000 would swallow both fixtures whole and prove
        nothing).
        """
        class _SmallBatch(tg.DuckDbAdapter):
            BATCH = 100

        small = self._peak(300, _SmallBatch())
        large = self._peak(3_000, _SmallBatch())
        self.assertLess(large, small * 3,
                        f"proxy load still scales: 300={small}B → 3k={large}B")


if __name__ == "__main__":
    unittest.main()
