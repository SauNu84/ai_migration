#!/usr/bin/env python3
"""SAU-432 — TargetAdapter seam tests.

Two tiers, both DB-free (no Postgres, no Snowflake account, no DuckDB *server* — the
DuckDB proxy runs in-process):

  * **DuckDbAdapter** — proves the default target reproduces the pre-seam
    `reconcile.run_medallion` / `read_gold` behaviour byte-for-byte (the §8 gate stays
    GREEN through the seam), and is the wired default.
  * **SnowflakeAdapter** — proves, against a *mock cursor*, that the live path:
      - sequences USE WAREHOUSE → Bronze → stage → PUT → COPY INTO → Silver → Gold;
      - runs the committed Snowflake SQL **byte-identical** (no dialect fixups);
      - keeps VARIANT/ARRAY native (PARSE_JSON, not JSON-text);
      - preserves the TIMESTAMP_NTZ vs TIMESTAMP_TZ distinction;
      - stays guarded (connect() refuses) until explicitly made live.

Run:
    .venv-test/bin/python -m unittest tests.test_target -v
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from core import ontology as ont
from core import mapper as mp
from core import codegen as cg
from core import reconcile as rc
from core import target as tg
from core import snowflake_conn as sc

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "artifacts" / "profile.json"
SQLDIR = ROOT / "artifacts" / "sql"


def _all_code() -> dict[str, cg.EntityCode]:
    """Lift + map + codegen the committed profile — the same path run_loop takes."""
    profile = json.loads(PROFILE.read_text())
    maps = mp.map_ontology(ont.lift(profile))
    return cg.generate(maps)


class _MockCursor:
    """Records every execute()'d statement; serves canned fetchall rows."""

    def __init__(self, rows: list[tuple] | None = None):
        self.executed: list[str] = []
        self._rows = rows or []

    def execute(self, sql: str):
        self.executed.append(sql)
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _MockConn:
    def __init__(self, cursor: _MockCursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _stmt_index(stmts: list[str], needle: str) -> int:
    for i, s in enumerate(stmts):
        if needle in s:
            return i
    return -1


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestDuckDbAdapter(unittest.TestCase):
    """The default target reproduces the legacy DuckDB execution exactly."""

    def setUp(self):
        self.code = _all_code()

    def test_default_target_is_the_duckdb_proxy(self):
        self.assertIsInstance(tg.DEFAULT_TARGET, tg.DuckDbAdapter)
        self.assertEqual(tg.DEFAULT_TARGET.name, "duckdb")

    def test_run_medallion_byte_for_byte_with_legacy_functions(self):
        # A minimal lossless wire row for app_user (all text, the bulk-load shape).
        ec = self.code["app_user"]
        rows = [{
            "created_at": "2024-01-01 09:30:00+00", "email": "a@b.com",
            "full_name": "Ada", "id": "1", "last_login_at": "2024-01-02 08:00:00",
            "org_id": "7", "phone": "555-0100", "preferences": '{"theme":"dark"}',
            "role": "admin", "ssn_last4": "0007",
        }]
        # Path A: through the adapter seam.
        adapter = tg.DuckDbAdapter()
        h = adapter.connect()
        try:
            adapter.run_medallion(h, ec, rows)
            gold_adapter = adapter.read_gold(h, ec)
        finally:
            adapter.disconnect(h)
        # Path B: the legacy module-level functions directly (the pre-seam code path).
        con = rc.duckdb.connect(":memory:")
        con.execute("SET TimeZone='UTC'")
        try:
            rc.run_medallion(con, ec, rows)
            gold_legacy = rc.read_gold(con, ec.entity, ec.text_columns)
        finally:
            con.close()
        self.assertEqual(gold_adapter, gold_legacy)
        self.assertEqual(len(gold_adapter), 1)   # the row round-tripped through Gold

    def test_reconcile_run_defaults_to_duckdb_without_a_target(self):
        # rc.run with no `target` must behave identically to an explicit DuckDbAdapter.
        # (Both build Bronze→Silver→Gold in-process; a synthetic single-entity code slice
        # keeps this DB-free — no source extraction needed for the empty-table case.)
        ec = self.code["plan"]
        # Use a source dialect stub that returns no rows so no Postgres is touched.
        stub = _NoRowsDialect()
        maps = [m for m in _all_maps() if m.entity == "plan"]
        r_default = rc.run({"plan": ec}, maps, _udt_index(), dialect=stub)
        r_explicit = rc.run({"plan": ec}, maps, _udt_index(), dialect=stub,
                            target=tg.DuckDbAdapter())
        self.assertEqual([e.entity for e in r_default], [e.entity for e in r_explicit])
        self.assertEqual(r_default[0].target_rows, r_explicit[0].target_rows)


class _NoRowsDialect:
    """A source dialect that extracts nothing — keeps rc.run DB-free for the seam test."""
    name = "norows"

    def extract_rows(self, entity, mappings):
        return []

    def fix_tz(self, s):
        return s

    def is_narrow_float(self, udt):
        return False

    def physical_token(self, data_type, udt):
        return ""


def _all_maps():
    return mp.map_ontology(ont.lift(json.loads(PROFILE.read_text())))


def _udt_index():
    onto = ont.lift(json.loads(PROFILE.read_text()))
    return {(e.name, p.name): p.udt for e in onto.entities for p in e.properties}


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestSnowflakeAdapterSequencing(unittest.TestCase):
    """The live medallion sequencing, exercised DB-free against a mock cursor."""

    def setUp(self):
        self.code = _all_code()
        self.ec = self.code["app_user"]           # VARIANT + NTZ + TZ in one entity
        self.rows = [{c: "x" for c in self.ec.text_columns}]

    def _run(self, adapter: tg.SnowflakeAdapter, ec=None):
        cur = _MockCursor()
        adapter.execute_medallion(cur, ec or self.ec, self.rows)
        return cur.executed

    def test_warehouse_and_copy_into_sequencing(self):
        stmts = self._run(tg.SnowflakeAdapter(warehouse="MIGRATION_WH"))
        i_wh = _stmt_index(stmts, "USE WAREHOUSE")
        i_bronze = _stmt_index(stmts, 'CREATE TABLE "bronze_app_user"')
        i_stage = _stmt_index(stmts, "CREATE TEMPORARY STAGE")
        i_put = _stmt_index(stmts, "PUT file://")
        i_copy = _stmt_index(stmts, "COPY INTO")
        i_silver = _stmt_index(stmts, 'CREATE TABLE "silver_app_user"')
        i_gold = _stmt_index(stmts, 'CREATE TABLE "gold_app_user"')
        # every step present …
        for name, idx in [("warehouse", i_wh), ("bronze", i_bronze), ("stage", i_stage),
                          ("put", i_put), ("copy", i_copy), ("silver", i_silver),
                          ("gold", i_gold)]:
            self.assertGreaterEqual(idx, 0, f"missing step: {name}")
        # … in strict order: wh < bronze < stage < put < copy < silver < gold
        self.assertLess(i_wh, i_bronze)
        self.assertLess(i_bronze, i_stage)
        self.assertLess(i_stage, i_put)
        self.assertLess(i_put, i_copy)
        self.assertLess(i_copy, i_silver)
        self.assertLess(i_silver, i_gold)
        self.assertEqual(sum("COPY INTO" in s for s in stmts), 1)

    def test_copy_into_targets_bronze_from_stage(self):
        stmts = self._run(tg.SnowflakeAdapter(warehouse="WH"))
        copy = next(s for s in stmts if "COPY INTO" in s)
        self.assertIn('"bronze_app_user"', copy)
        self.assertIn("@migration_bronze_stage/app_user.json", copy)
        self.assertIn("MATCH_BY_COLUMN_NAME", copy)     # column-order-independent load
        self.assertIn("TYPE = JSON", copy)

    def test_runs_committed_snowflake_sql_byte_identical(self):
        # The adapter must execute the codegen Snowflake statements verbatim — a single
        # introduced fixup would break the "runs the committed SQL unedited" contract.
        stmts = self._run(tg.SnowflakeAdapter(warehouse="WH"))
        self.assertIn(self.ec.bronze_ddl["snowflake"], stmts)
        self.assertIn(self.ec.silver_sql["snowflake"], stmts)
        self.assertIn(self.ec.gold_sql["snowflake"], stmts)

    def test_variant_and_array_stay_native_not_json_text(self):
        # app_user.preferences is VARIANT; customer_order.tags is ARRAY. Both must re-type
        # via PARSE_JSON (native semi-structured) — NOT be stringified to VARCHAR/TEXT.
        silver_user = next(s for s in self._run(tg.SnowflakeAdapter())
                           if 'silver_app_user' in s)
        self.assertIn('PARSE_JSON("preferences")', silver_user)
        order_ec = self.code["customer_order"]
        silver_order = order_ec.silver_sql["snowflake"]
        self.assertIn('PARSE_JSON("tags")', silver_order)            # ARRAY, native
        self.assertIn('PARSE_JSON("shipping_address")', silver_order)  # VARIANT, native
        # no JSON-text degradation of the semi-structured columns
        for bad in ('CAST("preferences" AS VARCHAR', 'TO_JSON', '::VARCHAR', '::TEXT'):
            self.assertNotIn(bad, silver_user)

    def test_timestamp_ntz_tz_distinction_preserved(self):
        silver = next(s for s in self._run(tg.SnowflakeAdapter()) if 'silver_app_user' in s)
        self.assertIn('CAST("created_at" AS TIMESTAMPTZ)', silver)       # tz-aware
        self.assertIn('CAST("last_login_at" AS TIMESTAMPNTZ)', silver)   # wall-clock
        # the two are genuinely distinct target types (not collapsed to one)
        self.assertNotIn('CAST("created_at" AS TIMESTAMPNTZ)', silver)
        self.assertNotIn('CAST("last_login_at" AS TIMESTAMPTZ)', silver)

    def test_no_warehouse_omits_use_warehouse(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("SNOWFLAKE_WAREHOUSE", None)
            stmts = self._run(tg.SnowflakeAdapter(warehouse=None))
        self.assertEqual(_stmt_index(stmts, "USE WAREHOUSE"), -1)
        self.assertGreaterEqual(_stmt_index(stmts, 'CREATE TABLE "bronze_app_user"'), 0)

    def test_empty_rows_skips_the_put_but_still_builds_medallion(self):
        cur = _MockCursor()
        tg.SnowflakeAdapter(warehouse="WH").execute_medallion(cur, self.ec, [])
        self.assertEqual(_stmt_index(cur.executed, "PUT file://"), -1)   # nothing to stage
        self.assertGreaterEqual(_stmt_index(cur.executed, "COPY INTO"), 0)
        self.assertGreaterEqual(_stmt_index(cur.executed, 'silver_app_user'), 0)


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestSnowflakeAdapterGuardAndLive(unittest.TestCase):
    def setUp(self):
        self.code = _all_code()

    def test_connect_is_guarded_until_live(self):
        with self.assertRaises(tg.TargetNotLiveError):
            tg.SnowflakeAdapter().connect()

    def test_live_connect_uses_snowflake_conn(self):
        sentinel = object()
        with mock.patch.object(sc, "connect", return_value=sentinel) as m:
            got = tg.SnowflakeAdapter(live=True).connect()
        self.assertIs(got, sentinel)
        m.assert_called_once()

    def test_reconcile_run_with_guarded_snowflake_surfaces_the_guard(self):
        # Selecting the live target before creds land must fail loudly at connect —
        # never silently fall back or half-run.
        maps = [m for m in _all_maps() if m.entity == "plan"]
        with self.assertRaises(tg.TargetNotLiveError):
            rc.run({"plan": self.code["plan"]}, maps, _udt_index(),
                   dialect=_NoRowsDialect(), target=tg.SnowflakeAdapter())

    def test_read_gold_orders_by_id_and_maps_columns(self):
        ec = self.code["app_user"]
        cols = ec.text_columns
        canned = [tuple(f"{c}0" for c in cols), tuple(f"{c}1" for c in cols)]
        cur = _MockCursor(canned)
        out = tg.SnowflakeAdapter(live=True).read_gold(_MockConn(cur), ec)
        self.assertEqual(out[0], {c: f"{c}0" for c in cols})
        self.assertIn('ORDER BY "id"', cur.executed[-1])   # PK-ordered read


@unittest.skipUnless(SQLDIR.exists(), "artifacts/sql not generated")
class TestCommittedArtifactsByteIdentical(unittest.TestCase):
    """The committed artifacts/sql/*.snowflake.sql are exactly what the adapter runs —
    the seam introduced no dialect fixups anywhere."""

    def test_every_committed_snowflake_artifact_matches_codegen(self):
        code = _all_code()
        checked = 0
        for f in sorted(SQLDIR.glob("*.snowflake.sql")):
            entity = f.name[: -len(".snowflake.sql")]
            ec = code[entity]
            expected = "\n\n".join([ec.bronze_ddl["snowflake"], ec.silver_sql["snowflake"],
                                    ec.gold_sql["snowflake"]]) + "\n"
            self.assertEqual(f.read_text(), expected, f"{f.name} drifted from codegen")
            checked += 1
        self.assertGreaterEqual(checked, 15)   # the ≥15-table schema


if __name__ == "__main__":
    unittest.main(verbosity=2)
