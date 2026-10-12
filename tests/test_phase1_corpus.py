"""SAU-532 — the Phase-1 corpus generator (corpus/generate.py).

Acceptance, as tests:
  1. byte-identical across runs from a fixed seed (and different for a different seed);
  2. the SAU-480 shape — ≥25 entities, ≥3 with no unique `id`, ≥50 % of rows in one;
  3. the seven trap types Phase 0 never seeded are present as hand-authored VALUES rows
     that bullet 0 can parse, and every manifest column is one of them;
  4/5 are runs, not tests — see GATE_READOUT.phase1.md and the ticket.

The DB tier (load twice, compare in-database content digests) needs a reachable Postgres
and is opt-in via SAU532_DB_TESTS=1 because it creates and drops a schema.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from corpus import generate as gen  # noqa: E402
from core import corpus as cp  # noqa: E402

MANIFEST = ROOT / "ground_truth" / "hard_columns.phase1.json"


class TestDeterminism(unittest.TestCase):
    def test_same_seed_same_bytes(self):
        a = gen.build(50_000, seed=7)
        b = gen.build(50_000, seed=7)
        self.assertEqual(a.full_sql, b.full_sql)
        self.assertEqual(a.manifest()["sha256_sql"], b.manifest()["sha256_sql"])

    def test_different_seed_different_bytes(self):
        a = gen.build(50_000, seed=7)
        b = gen.build(50_000, seed=8)
        self.assertNotEqual(a.full_sql, b.full_sql)
        # …but the trap rows are hand-authored literals and do not move with the seed.
        self.assertEqual(a.trap_sql, b.trap_sql)

    def test_no_nondeterministic_sql(self):
        sql = gen.build(50_000).full_sql.lower()
        for bad in ("random(", "now()", "clock_timestamp", "current_timestamp",
                    "gen_random_uuid", "nextval(", "serial"):
            self.assertNotIn(bad, sql, f"{bad!r} would make the corpus differ between runs")

    def test_ordinals_are_bigint(self):
        # g * a1 + b1 exceeds int4 at 10M rows; every fill must widen the ordinal.
        sql = gen.build(50_000).volume_sql
        self.assertNotIn("generate_series(1, ", sql)
        self.assertGreaterEqual(sql.count("generate_series(1::bigint, "), 20)


class TestShape(unittest.TestCase):
    def setUp(self):
        self.c = gen.build(1_000_000)
        self.m = self.c.manifest()

    def test_at_least_25_entities(self):
        self.assertGreaterEqual(self.m["shape"]["entities"], 25)
        self.assertEqual(self.m["shape"]["entities"], len(self.m["entities"]))

    def test_at_least_3_entities_without_unique_id(self):
        no_id = self.m["shape"]["entities_without_unique_id"]
        self.assertGreaterEqual(len(no_id), 3)
        for name in no_id:
            ddl = re.search(rf"CREATE TABLE \w+\.{name} \((.*?)\);", self.c.ddl_sql, re.S).group(1)
            self.assertNotRegex(ddl, r"^\s*id\s", f"{name} must not carry an id column")
            self.assertNotIn("PRIMARY KEY", ddl, f"{name} must not carry a PK")

    def test_dominant_entity_holds_half_the_rows(self):
        self.assertGreaterEqual(self.m["shape"]["dominant_share"], 0.5)
        self.assertEqual(self.m["shape"]["dominant_entity"], "event")

    def test_total_is_exact(self):
        self.assertEqual(sum(self.c.plan.values()), 1_000_000)
        self.assertEqual(self.m["volume_rows"], 1_000_000)

    def test_gate_tier_scales_proportionally(self):
        g = gen.build(10_000_000).manifest()
        self.assertGreaterEqual(g["shape"]["dominant_rows"], 5_000_000)

    def test_duplicate_rows_entity_is_declared(self):
        e = next(e for e in self.m["entities"] if e["name"] == "metric_sample")
        self.assertFalse(e["has_unique_id"])
        self.assertIn("duplicate", e["note"])

    def test_matches_committed_reference(self):
        # The reference manifest is the acceptance record for "byte-identical across
        # runs": it pins the SQL digest of the 1M proxy-tier corpus at the default seed.
        # A generator change that moves it must update the reference consciously — and
        # invalidates every recorded in-DB content digest with it.
        ref = json.loads((ROOT / "corpus" / "reference"
                          / "corpus_manifest.seed20260921.1m.json").read_text())
        self.assertEqual(self.m["seed"], ref["seed"])
        self.assertEqual(self.m["sha256_sql"], ref["sha256_sql"])
        self.assertEqual(self.m["sha256_trap_sql"], ref["sha256_trap_sql"])
        self.assertEqual({e["name"]: e["rows"] for e in self.m["entities"]},
                         {k: v["rows"] for k, v in ref["content_digests"].items()})

    def test_too_small_is_refused(self):
        with self.assertRaises(SystemExit):
            gen.build(2_000)


class TestTraps(unittest.TestCase):
    def setUp(self):
        self.c = gen.build(50_000)
        self.manifest = json.loads(MANIFEST.read_text())

    def test_seven_missing_types_are_declared(self):
        ddl = gen._trap_ddl(gen.DEFAULT_SCHEMA)
        for t in gen.TRAP_TYPES:
            self.assertRegex(ddl, rf"\b{t}\b", f"{t} missing from the trap DDL")

    def test_trap_rows_are_values_only_and_parse(self):
        # Bullet 0 only audits VALUES literals; an INSERT … SELECT states no claim.
        self.assertNotIn("SELECT", self.c.trap_sql.upper().replace("INSERT", ""))
        claims = cp.seed_claims(self._trap_path(), read="postgres")
        for t in gen.TRAP_ENTITIES:
            self.assertTrue(any(k[0] == t for k in claims), f"no parsed INSERT for {t}")

    def test_every_manifest_column_is_hand_authored(self):
        claims = cp.seed_claims(self._trap_path(), read="postgres")
        for h in self.manifest["hard_columns"]:
            self.assertIn((h["table"], h["column"]), claims,
                          f"{h['table']}.{h['column']} is not in a VALUES-only INSERT, so "
                          f"bullet 0 cannot audit it")
            self.assertIn(h["table"], gen.TRAP_ENTITIES)

    def test_manifest_covers_all_seven_types(self):
        types = {h["pg_type"].split("(")[0].upper() for h in self.manifest["hard_columns"]}
        for t in gen.TRAP_TYPES:
            self.assertIn(t, types, f"manifest has no hard column of type {t}")

    def _trap_path(self) -> Path:
        p = Path("/tmp") / f"sau532_traps_{os.getpid()}.sql"
        p.write_text(self.c.trap_sql)
        self.addCleanup(p.unlink)
        return p


def _pg_reachable() -> bool:
    try:
        return subprocess.run(["psql", "-X", "-tAc", "SELECT 1"], capture_output=True,
                              text=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(os.environ.get("SAU532_DB_TESTS") == "1" and _pg_reachable(),
                     "set SAU532_DB_TESTS=1 with a reachable Postgres (creates/drops a schema)")
class TestInDatabase(unittest.TestCase):
    SCHEMA = "sau532_test"

    def tearDown(self):
        subprocess.run(["psql", "-X", "-c", f"DROP SCHEMA IF EXISTS {self.SCHEMA} CASCADE"],
                       capture_output=True)

    def test_two_loads_have_identical_content_digests(self):
        a = gen.apply(gen.build(20_000, schema=self.SCHEMA))
        b = gen.apply(gen.build(20_000, schema=self.SCHEMA))
        self.assertEqual(a, b)
        self.assertEqual(sum(v["rows"] for v in a.values()), 20_000 + 4 * len(gen.TRAP_ENTITIES))

    def test_duplicate_rows_really_are_duplicates(self):
        gen.apply(gen.build(20_000, schema=self.SCHEMA))
        out = subprocess.run(["psql", "-X", "-tAc",
                              f"SELECT count(*), count(DISTINCT t::text) FROM {self.SCHEMA}.metric_sample t"],
                             capture_output=True, text=True).stdout.strip()
        n, d = (int(x) for x in out.split("|"))
        self.assertGreater(n, d, "metric_sample must contain exact-duplicate rows")


if __name__ == "__main__":
    unittest.main()
