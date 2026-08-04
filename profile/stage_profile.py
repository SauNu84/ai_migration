#!/usr/bin/env python3
"""SAU-514 — stage-attribution profiler for the verification loop.

**What this answers.** SAU-511 measured the whole loop at 81.2 s @10k and 962.5 s @100k
for one 10-column entity, but *which stage* dominates was never measured. Four of the six
stages run on the DuckDB **execution proxy** (`core/target.py`), which SAU-432's
`TargetAdapter` seam exists to replace with live Snowflake. So an unattributed total does
not say whether it is a number about our chassis or a number about DuckDB. This profiler
splits the wall clock per stage and tags each stage by whether it survives that swap.

**Why instrumentation and not a rewritten loop.** Since SAU-511 the source read, the
source digest and the Bronze load are *interleaved*: `reconcile.run` tees one row walk
into `EntityDigest.add` on its way into a batched `executemany`. There is no point in the
code where "extraction" finishes and "load" begins, so per-stage cost cannot be recovered
by timing coarse call boundaries — it has to be accumulated per row at the exact seams.
A re-implemented loop with timers in convenient places would measure a different program:
that is the SAU-511 mistake (a synthetic dialect has no reader, so it could not measure
the reader) repeated on the time axis. Everything below therefore wraps the **real**
`reconcile.run` via subclasses and a connection proxy; no core module is edited and no
executed SQL changes.

**Stages** (non-overlapping by construction — each is timed at exactly one nesting level,
so they sum to at most the total and the remainder is honest chassis overhead):

| stage           | where                                              | survives Snowflake? |
|-----------------|----------------------------------------------------|---------------------|
| `extract_pull`  | `next()` on `PostgresDialect.iter_rows` (psql pipe + `json.loads`) | **durable** |
| `canon_source`  | `EntityDigest.add` during the load (source side)    | **durable** |
| `bronze_ddl`    | `CREATE TABLE bronze_…`                             | proxy |
| `bronze_load`   | `executemany` of the batched INSERT                 | proxy (becomes COPY INTO) |
| `silver`        | `CREATE TABLE silver_… AS SELECT` (typed CASTs)     | proxy |
| `gold`          | `CREATE TABLE gold_… AS SELECT`                     | proxy |
| `gold_readback` | `next()` on `iter_gold` (query + `fetchmany` + dict) | proxy |
| `canon_target`  | `EntityDigest.add` on Gold rows (target side)       | **durable in kind** |

`canon_target` is deliberately **not** filed under "proxy" the way the ticket's table has
it. The Gold *read* is the proxy's (it becomes a Snowflake cursor); the digest fold is our
Python `canonicalize()` and runs byte-identically on rows that came from Snowflake. Its
cost moves engines, it does not disappear. Reporting it as proxy would understate the
durable half — the whole point of the exercise is not to do that.

**A caveat that is a property of the measurement, not a defect.** `extract_pull` is time
Python is *blocked* on the psql pipe. psql runs concurrently, so while Python is loading
Bronze or canonicalizing, the server keeps filling the pipe buffer. `extract_pull` is
therefore the source's contribution *to the critical path*, which is the number that
matters for "what does the run cost", and an under-estimate of the server's total work.
Stated rather than hidden.

Usage:
    PGHOST=/tmp PGPORT=5599 PGUSER=postgres PGDATABASE=phase0 \
        python3 profile/stage_profile.py --rows 100000 --out artifacts/stage_profile.json
    ... --sort-ab            # ORDER BY ablation only (the n·log n hypothesis)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core import codegen as cg          # noqa: E402
from core import dialect as dl          # noqa: E402
from core import mapper as mp           # noqa: E402
from core import ontology as ont        # noqa: E402
from core import reconcile as rc        # noqa: E402
from core import target as tg           # noqa: E402

# ── stage taxonomy ───────────────────────────────────────────────────────────
# side: "durable"  — source-side, survives the DuckDB→Snowflake swap unchanged
#       "proxy"    — target-side, replaced when SnowflakeAdapter goes live
#       "durable*" — our Python, but fed by target rows: moves engines, does not vanish
STAGE_SIDE = OrderedDict([
    ("extract_pull", "durable"),
    ("canon_source", "durable"),
    ("bronze_ddl", "proxy"),
    ("bronze_load", "proxy"),
    ("silver", "proxy"),
    ("gold", "proxy"),
    ("gold_readback", "proxy"),
    ("canon_target", "durable*"),
    ("session", "proxy"),
])

# sub-measures nested INSIDE a top-level stage — reported for detail, never summed with
# the top-level stages (that would double count).
NESTED = {"gold_select": "gold_readback", "gold_fetch": "gold_readback"}


class StageClock:
    """Accumulates wall-clock and call counts per stage name."""

    def __init__(self) -> None:
        self.t: dict[str, float] = {}
        self.n: dict[str, int] = {}

    def add(self, stage: str, dt: float, n: int = 1) -> None:
        self.t[stage] = self.t.get(stage, 0.0) + dt
        self.n[stage] = self.n.get(stage, 0) + n

    def top_level_total(self) -> float:
        return sum(v for k, v in self.t.items() if k not in NESTED)


# ── source seam: time each row pulled off the psql pipe ──────────────────────

class TimedPostgresDialect(dl.PostgresDialect):
    """`PostgresDialect` with the row pull timed. The extraction SQL, the psql argv and
    the cursor-backed FETCH_COUNT streaming are all inherited verbatim — only the
    generator that yields the parsed rows is wrapped."""

    def __init__(self, clock: StageClock) -> None:
        super().__init__()
        self._clock = clock

    def iter_rows(self, entity: str, mappings: list[Any]) -> Iterator[dict[str, Any]]:
        inner = super().iter_rows(entity, mappings)

        def gen() -> Iterator[dict[str, Any]]:
            while True:
                t0 = time.perf_counter()
                try:
                    row = next(inner)
                except StopIteration:
                    self._clock.add("extract_pull", time.perf_counter() - t0, 0)
                    return
                # An extraction failure must still surface as itself; `_tee` tags it
                # SourceReadError. Timing must not swallow or reclassify it.
                self._clock.add("extract_pull", time.perf_counter() - t0)
                yield row

        return gen()


# ── target seam: classify every statement the medallion executes ─────────────

def _classify(sql: str) -> str:
    s = sql.lstrip()
    head = s[:64].upper()
    if head.startswith("SET "):
        return "session"
    if head.startswith("CREATE TABLE"):
        if '"bronze_' in s[:64]:
            return "bronze_ddl"
        if '"silver_' in s[:64]:
            return "silver"
        if '"gold_' in s[:64]:
            return "gold"
    if head.startswith("SELECT"):
        return "gold_select"          # the Gold read-back query (holds the ORDER BY)
    return "other"


class TimedConnection:
    """Proxy over the DuckDB connection that times every `execute`/`executemany`/fetch.

    DuckDB's `execute()` returns the connection itself, and `reconcile.iter_gold` relies
    on that (`cur = con.execute(...)` then `cur.fetchmany(...)`). The proxy preserves the
    idiom by returning *itself*, which is also what puts the fetch inside the timing.
    """

    def __init__(self, con: Any, clock: StageClock) -> None:
        self._con = con
        self._clock = clock

    def execute(self, sql: str, *a: Any, **k: Any) -> "TimedConnection":
        stage = _classify(sql)
        t0 = time.perf_counter()
        self._con.execute(sql, *a, **k)
        self._clock.add(stage, time.perf_counter() - t0)
        return self

    def executemany(self, sql: str, data: Any, *a: Any, **k: Any) -> "TimedConnection":
        rows = len(data) if hasattr(data, "__len__") else 0
        t0 = time.perf_counter()
        self._con.executemany(sql, data, *a, **k)
        self._clock.add("bronze_load", time.perf_counter() - t0, rows)
        return self

    def fetchmany(self, n: int) -> Any:
        t0 = time.perf_counter()
        out = self._con.fetchmany(n)
        self._clock.add("gold_fetch", time.perf_counter() - t0, len(out))
        return out

    def fetchall(self) -> Any:
        t0 = time.perf_counter()
        out = self._con.fetchall()
        self._clock.add("gold_fetch", time.perf_counter() - t0, len(out))
        return out

    def close(self) -> None:
        self._con.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


class TimedDuckDbAdapter(tg.DuckDbAdapter):
    """`DuckDbAdapter` with a timing connection and a phase flag.

    The phase flag is what makes source- and target-side canonicalization separable.
    `reconcile.run` builds two `EntityDigest`s per entity and feeds one from inside
    `load_medallion` (the tee) and the other from the Gold walk afterwards. Rather than
    guessing which digest is which by construction order — which breaks the moment an
    entity errors before its target digest exists — the adapter simply records which
    phase the loop is in, and every `add()` is attributed to that. Exact, and it stays
    exact on a failed entity.
    """

    def __init__(self, clock: StageClock, phase: list[str]) -> None:
        self._clock = clock
        self._phase = phase

    def connect(self) -> Any:
        return TimedConnection(super().connect(), self._clock)

    def load_medallion(self, handle: Any, ec: "cg.EntityCode", rows: Any) -> None:
        self._phase[0] = "canon_source"
        try:
            super().load_medallion(handle, ec, rows)
        finally:
            self._phase[0] = "canon_target"

    def iter_gold(self, handle: Any, ec: "cg.EntityCode") -> Iterator[dict[str, Any]]:
        inner = super().iter_gold(handle, ec)

        def gen() -> Iterator[dict[str, Any]]:
            while True:
                t0 = time.perf_counter()
                try:
                    row = next(inner)
                except StopIteration:
                    self._clock.add("gold_readback", time.perf_counter() - t0, 0)
                    return
                self._clock.add("gold_readback", time.perf_counter() - t0)
                yield row

        return gen()


def make_timed_digest(base: type, clock: StageClock, phase: list[str]) -> type:
    """Subclass `EntityDigest` so the canonicalize+checksum fold is timed in place.

    A subclass (not a rewrite) keeps the measured body the production body: `add()` still
    runs the real `canonicalize()` for the real reconcile strategy of every column.
    """

    class TimedEntityDigest(base):  # type: ignore[valid-type,misc]
        def add(self, row: dict[str, Any]) -> None:
            t0 = time.perf_counter()
            super().add(row)
            clock.add(phase[0], time.perf_counter() - t0)

    return TimedEntityDigest


# ── probe corpus (own schema, never touches the seed — SAU-511 method) ───────

PROBE_DDL = """
CREATE SCHEMA {schema};
CREATE TABLE {schema}.app_user (
  id            BIGINT PRIMARY KEY,
  org_id        BIGINT NOT NULL,
  email         VARCHAR(320) NOT NULL,
  full_name     VARCHAR(200),
  role          public.user_role NOT NULL DEFAULT 'member',
  phone         VARCHAR(32),
  ssn_last4     CHAR(4),
  preferences   JSONB,
  last_login_at TIMESTAMP,
  created_at    TIMESTAMPTZ NOT NULL
);
"""

# Same 10 columns and same physical types as the seed's `app_user` (the 10-column mixed
# entity SAU-511 measured): two timestamps of *different* tz-awareness, JSONB, an enum,
# a fixed-width CHAR, two BIGINTs, three VARCHARs. Nulls at a few rates so the NULL
# sentinel path in the checksum is exercised too.
PROBE_FILL = """
INSERT INTO {schema}.app_user
SELECT g,
       (g % 1000) + 1,
       'user' || g || '@example.com',
       CASE WHEN g % 7 = 0 THEN NULL ELSE 'Person Number ' || g END,
       (ARRAY['owner','admin','member','billing','readonly']::public.user_role[])[(g % 5) + 1],
       CASE WHEN g % 5 = 0 THEN NULL ELSE '+1-555-' || lpad((g % 10000)::text, 4, '0') END,
       lpad((g % 10000)::text, 4, '0'),
       jsonb_build_object('theme', CASE WHEN g % 2 = 0 THEN 'dark' ELSE 'light' END,
                          'digest', (g % 3 = 0), 'rank', g),
       CASE WHEN g % 11 = 0 THEN NULL
            ELSE timestamp '2024-01-01 00:00:00' + (g || ' seconds')::interval END,
       timestamptz '2024-01-01 00:00:00+00' + (g || ' seconds')::interval
FROM generate_series(1, {rows}) g;
"""


def psql(sql: str, *, env: dict[str, str], context: str = "psql") -> str:
    proc = subprocess.run(["psql", "-X", "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"{context} failed (exit {proc.returncode})")
    return proc.stdout


def build_probe(schema: str, rows: int, env: dict[str, str]) -> None:
    psql(f"DROP SCHEMA IF EXISTS {schema} CASCADE;", env=env, context="drop probe")
    psql(PROBE_DDL.format(schema=schema), env=env, context="create probe")
    psql(PROBE_FILL.format(schema=schema, rows=rows), env=env, context="fill probe")
    psql(f"ANALYZE {schema}.app_user;", env=env, context="analyze probe")


def drop_probe(schema: str, env: dict[str, str]) -> None:
    psql(f"DROP SCHEMA IF EXISTS {schema} CASCADE;", env=env, context="drop probe")


def codegen_for(schema: str, dialect: dl.SourceDialect) -> tuple[dict, list, dict]:
    """Real lift → freeze → map → codegen against the probe schema."""
    raw = dialect.run_profiler(schema)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write(raw)
        path = fh.name
    try:
        profile = ont.load_profile(path)
    finally:
        os.unlink(path)
    onto = ont.lift(profile, dialect=dialect)
    frozen = ont.freeze_ontology(onto)
    with ont.assert_no_inference():
        maps = mp.map_ontology(ont.load_frozen(frozen))
        code = cg.generate(maps)
    udt_index = {(e.name, p.name): p.udt for e in onto.entities for p in e.properties}
    return code, maps, udt_index


# ── the profile run ──────────────────────────────────────────────────────────

def profile_run(rows: int, schema: str, env: dict[str, str],
                *, tracemalloc_on: bool = False) -> dict[str, Any]:
    """One instrumented run. `tracemalloc_on` reproduces the *measurement conditions* of
    the SAU-511 memory probe, which is a separate question from the loop's own cost —
    see the `--tracemalloc` note in `main`."""
    clock = StageClock()
    phase = ["canon_source"]
    dialect = TimedPostgresDialect(clock)
    target = TimedDuckDbAdapter(clock, phase)

    code, maps, udt_index = codegen_for(schema, dialect)

    original_digest = rc.EntityDigest
    rc.EntityDigest = make_timed_digest(original_digest, clock, phase)
    peak = None
    if tracemalloc_on:
        import tracemalloc
        tracemalloc.start()
    try:
        t0 = time.perf_counter()
        results = rc.run(code, maps, udt_index, dialect=dialect, target=target)
        total = time.perf_counter() - t0
        if tracemalloc_on:
            import tracemalloc
            peak = tracemalloc.get_traced_memory()[1] / 1e6
            tracemalloc.stop()
    finally:
        rc.EntityDigest = original_digest

    res = results[0]
    stages = clock.top_level_total()
    return {
        "rows": rows,
        "tracemalloc": tracemalloc_on,
        "peak_mb": peak,
        "entity": res.entity,
        "columns": len(res.columns),
        "source_rows": res.source_rows,
        "target_rows": res.target_rows,
        "row_match": res.row_match,
        "all_columns_match": all(c.match for c in res.columns),
        "exec_error": res.exec_error,
        "total_s": total,
        "stage_sum_s": stages,
        "residual_s": total - stages,
        "stages": {k: {"s": clock.t[k], "n": clock.n[k], "side": STAGE_SIDE.get(k, "?")}
                   for k in clock.t if k not in NESTED},
        "nested": {k: {"s": clock.t[k], "n": clock.n[k], "within": NESTED[k]}
                   for k in clock.t if k in NESTED},
    }


# ── ORDER BY ablation (the n·log n hypothesis, killed or confirmed cheaply) ──

def _drain(it: Iterator[Any]) -> tuple[int, float]:
    t0 = time.perf_counter()
    n = sum(1 for _ in it)
    return n, time.perf_counter() - t0


class NoSortDialect(dl.PostgresDialect):
    """`PostgresDialect` with the extraction's `ORDER BY "id"` removed — and nothing else.

    The ablation is the *only* difference: same projection, same per-column extraction
    expressions, same psql flags, same cursor streaming. Anything else differing would
    make the A/B measure two programs instead of one sort.
    """

    def _extract_sql(self, entity: str, mappings: list[Any]) -> str:
        return super()._extract_sql(entity, mappings).replace(' ORDER BY "id"', " ")


def sort_ab(rows: int, schema: str, env: dict[str, str]) -> dict[str, Any]:
    sorted_d = dl.PostgresDialect()
    unsorted_d = NoSortDialect()
    code, maps, udt_index = codegen_for(schema, sorted_d)
    ec = code["app_user"]
    ms = [m for m in maps if m.entity == "app_user"]

    # (a) source extraction, with and without the ORDER BY.
    n_s, t_sorted = _drain(sorted_d.iter_rows("app_user", ms))
    n_u, t_unsorted = _drain(unsorted_d.iter_rows("app_user", ms))

    # (b) the Gold read-back query, with and without its ORDER BY, over a medallion that
    #     is already built (so only the SELECT is being compared).
    con = tg.DuckDbAdapter().connect()
    try:
        rc.load_medallion(con, ec, sorted_d.iter_rows("app_user", ms), batch=5_000)
        cols = ec.text_columns
        quoted = ", ".join(f'"{c}"' for c in cols)
        g_sorted = f'SELECT {quoted} FROM "gold_app_user" ORDER BY "id"'
        g_unsorted = f'SELECT {quoted} FROM "gold_app_user" '
        t0 = time.perf_counter()
        n_gs = len(con.execute(g_sorted).fetchall())
        t_gold_sorted = time.perf_counter() - t0
        t0 = time.perf_counter()
        n_gu = len(con.execute(g_unsorted).fetchall())
        t_gold_unsorted = time.perf_counter() - t0
    finally:
        con.close()

    return {
        "rows": rows,
        "extract_sorted_s": t_sorted, "extract_unsorted_s": t_unsorted,
        "extract_rows": [n_s, n_u],
        "gold_sorted_s": t_gold_sorted, "gold_unsorted_s": t_gold_unsorted,
        "gold_rows": [n_gs, n_gu],
    }


# ── report ───────────────────────────────────────────────────────────────────

def render(runs: list[dict[str, Any]], ab: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for r in runs:
        tm = "  [UNDER TRACEMALLOC]" if r.get("tracemalloc") else ""
        peak = f"  peak {r['peak_mb']:.2f} MB" if r.get("peak_mb") else ""
        out.append(f"\n=== {r['entity']}  {r['rows']:,} rows x {r['columns']} cols  "
                   f"total {r['total_s']:.1f}s  "
                   f"({r['total_s'] / r['rows'] * 1000:.3f} ms/row){tm}{peak} ===")
        out.append(f"{'stage':<16}{'side':<10}{'seconds':>10}{'share':>9}{'us/row':>10}")
        items = sorted(r["stages"].items(), key=lambda kv: -kv[1]["s"])
        for name, d in items:
            out.append(f"{name:<16}{d['side']:<10}{d['s']:>10.2f}"
                       f"{d['s'] / r['total_s'] * 100:>8.1f}%{d['s'] / r['rows'] * 1e6:>10.1f}")
        out.append(f"{'residual':<16}{'chassis':<10}{r['residual_s']:>10.2f}"
                   f"{r['residual_s'] / r['total_s'] * 100:>8.1f}%")
        dur = sum(d["s"] for d in r["stages"].values() if d["side"].startswith("durable"))
        prx = sum(d["s"] for d in r["stages"].values() if d["side"] == "proxy")
        out.append(f"  durable {dur:.1f}s ({dur / r['total_s'] * 100:.1f}%)   "
                   f"proxy {prx:.1f}s ({prx / r['total_s'] * 100:.1f}%)")
        for name, d in r["nested"].items():
            out.append(f"    [{name} nested in {d['within']}] {d['s']:.2f}s")
        out.append(f"  verdict: row_match={r['row_match']} "
                   f"all_columns_match={r['all_columns_match']} exec_error={r['exec_error']}")

    if len(runs) > 1:
        # Per-row cost across the ladder. A stage that is O(n) shows a FLAT column; the
        # n·log n super-linearity hypothesis predicts a rising one. Reading it per row
        # rather than as a total is what makes the two visually distinguishable.
        ladder = sorted(runs, key=lambda r: r["rows"])
        out.append("\n=== per-row cost (us/row) across the ladder ===")
        head = "".join(f"{r['rows']:>12,}" for r in ladder)
        out.append(f"{'stage':<16}{head}{'first→last':>12}")
        names = sorted({n for r in ladder for n in r["stages"]})
        for name in names:
            cells, vals = "", []
            for r in ladder:
                v = r["stages"].get(name, {}).get("s", 0.0) / r["rows"] * 1e6
                vals.append(v)
                cells += f"{v:>12.1f}"
            chg = (vals[-1] / vals[0] - 1) * 100 if vals[0] else float("nan")
            out.append(f"{name:<16}{cells}{chg:>11.1f}%")
        vals = [r["total_s"] / r["rows"] * 1e6 for r in ladder]
        cells = "".join(f"{v:>12.1f}" for v in vals)
        out.append(f"{'TOTAL':<16}{cells}{(vals[-1] / vals[0] - 1) * 100:>11.1f}%")

    for r in ab:
        out.append(f"\n=== ORDER BY ablation @ {r['rows']:,} rows ===")
        de = r["extract_sorted_s"] - r["extract_unsorted_s"]
        dg = r["gold_sorted_s"] - r["gold_unsorted_s"]
        out.append(f"  source extract   sorted {r['extract_sorted_s']:.2f}s  "
                   f"unsorted {r['extract_unsorted_s']:.2f}s  delta {de:+.2f}s "
                   f"({de / r['extract_sorted_s'] * 100:+.1f}%)")
        out.append(f"  gold read-back   sorted {r['gold_sorted_s']:.3f}s  "
                   f"unsorted {r['gold_unsorted_s']:.3f}s  delta {dg:+.3f}s")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="SAU-514 stage-attribution profiler")
    ap.add_argument("--rows", type=int, action="append",
                    help="row count (repeatable; two values give the scaling table)")
    ap.add_argument("--schema", default="stage_probe")
    ap.add_argument("--sort-ab", action="store_true",
                    help="also run the ORDER BY ablation at each row count")
    ap.add_argument("--ab-only", action="store_true", help="ablation only, no stage profile")
    ap.add_argument("--tracemalloc", action="store_true",
                    help="run under tracemalloc — reproduces the SAU-511 memory probe's "
                         "measurement conditions, NOT the loop's own cost")
    ap.add_argument("--out", default=str(REPO / "artifacts" / "stage_profile.json"))
    ap.add_argument("--keep", action="store_true", help="do not drop the probe schema")
    args = ap.parse_args()
    row_counts = args.rows or [10_000, 100_000]

    env = dict(os.environ)
    # The extraction SQL is unqualified (`FROM "app_user"`), so the probe schema is
    # reached via search_path — `public` stays untouched (SAU-511 method).
    env["PGOPTIONS"] = f"-c search_path={args.schema},public"

    runs: list[dict[str, Any]] = []
    abs_: list[dict[str, Any]] = []
    for n in row_counts:
        build_probe(args.schema, n, env)
        os.environ["PGOPTIONS"] = env["PGOPTIONS"]   # the dialect shells out via os.environ
        try:
            if not args.ab_only:
                runs.append(profile_run(n, args.schema, env,
                                        tracemalloc_on=args.tracemalloc))
            if args.sort_ab or args.ab_only:
                abs_.append(sort_ab(n, args.schema, env))
        finally:
            if not args.keep:
                drop_probe(args.schema, env)

    doc = {
        "issue": "SAU-514",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "duckdb": __import__("duckdb").__version__,
        "batch": tg.DuckDbAdapter.BATCH,
        "fetch_count": dl.PostgresDialect.FETCH_COUNT,
        "runs": runs,
        "sort_ab": abs_,
    }
    Path(args.out).write_text(json.dumps(doc, indent=2) + "\n")
    print(render(runs, abs_))
    print(f"\n[stage_profile] → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
