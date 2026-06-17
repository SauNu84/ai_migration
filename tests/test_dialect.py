#!/usr/bin/env python3
"""SAU-322 SourceDialect seam tests.

Prove the source-half of the loop is behind one pluggable interface:
  * `PostgresDialect` reproduces today's hard-wired physical-type recognition
    (the canonical PT_* token table) and the tz/float canonicalization hooks;
  * the inference pass branches on the *canonical token*, so swapping the dialect
    swaps source recognition without touching `ontology.infer_property`;
  * the default lane is Postgres, so `lift()` with no dialect == `lift(PostgresDialect())`.

These are DB-free; the DB-backed proof that the full §8 gate stays GREEN through the
seam lives in tests.test_core_loop.TestVerificationLoopDB.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from core import dialect as dl
from core import ontology as ont
from core import mapper as mp
from core import reconcile as rc

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "artifacts" / "profile.json"


class TestPostgresPhysicalTokenTable(unittest.TestCase):
    def setUp(self):
        self.d = dl.PostgresDialect()

    def test_name(self):
        self.assertEqual(self.d.name, "postgres")

    def test_token_classification(self):
        cases = [
            # (data_type, udt, expected token)
            ("ARRAY", "_int8", dl.PT_ARRAY_NUMERIC),
            ("ARRAY", "_text", dl.PT_ARRAY),
            ("jsonb", "jsonb", dl.PT_JSON),
            ("USER-DEFINED", "inet", dl.PT_NETWORK),  # udt drives network
            ("timestamp with time zone", "timestamptz", dl.PT_TIMESTAMP_TZ),
            ("timestamp without time zone", "timestamp", dl.PT_TIMESTAMP_NTZ),
            ("date", "date", dl.PT_DATE),
            ("boolean", "bool", dl.PT_BOOLEAN),
            ("character", "bpchar", dl.PT_FIXED_CHAR),
            ("real", "float4", dl.PT_FLOAT_NARROW),
            ("double precision", "float8", dl.PT_FLOAT),
            ("numeric", "numeric", dl.PT_NUMERIC),
            ("bigint", "int8", dl.PT_INTEGER),
            ("character varying", "varchar", dl.PT_TEXT),
            ("text", "text", dl.PT_TEXT),
            ("geometry", "geometry", dl.PT_UNKNOWN),
        ]
        for dt, udt, expected in cases:
            self.assertEqual(self.d.physical_token(dt, udt), expected, f"{dt}/{udt}")

    def test_user_defined_enum_detection(self):
        self.assertTrue(self.d.is_user_defined("USER-DEFINED"))
        self.assertFalse(self.d.is_user_defined("text"))

    def test_narrow_float_token(self):
        self.assertTrue(self.d.is_narrow_float("float4"))
        self.assertTrue(self.d.is_narrow_float("real"))
        self.assertFalse(self.d.is_narrow_float("float8"))

    def test_fix_tz_wire_format(self):
        self.assertEqual(self.d.fix_tz("2024-01-01T00:00:00+00"), "2024-01-01T00:00:00+00:00")
        self.assertEqual(self.d.fix_tz("2024-01-01T00:00:00+02"), "2024-01-01T00:00:00+02:00")
        # already-normalized / no-offset values pass through untouched
        self.assertEqual(self.d.fix_tz("2024-01-01T00:00:00+00:00"), "2024-01-01T00:00:00+00:00")
        self.assertEqual(self.d.fix_tz("2024-01-01T00:00:00"), "2024-01-01T00:00:00")

    def test_extraction_expr_wire_form(self):
        self.assertEqual(self.d.extraction_expr("price", mp.RC_EXACT_TEXT), '"price"::text')
        self.assertEqual(self.d.extraction_expr("doc", mp.RC_JSON_CANONICAL), '"doc"::text')
        self.assertIn("json_agg", self.d.extraction_expr("tags", mp.RC_ARRAY_CANONICAL))


class TestDialectIsPluggable(unittest.TestCase):
    def test_default_lane_is_postgres(self):
        self.assertIsInstance(dl.DEFAULT_DIALECT, dl.PostgresDialect)

    def test_inference_branches_on_canonical_token_not_raw_type(self):
        # A stub dialect that forces every column to look like a JSON column must flip
        # inference to semi_structured — proving infer_property reads the dialect token,
        # never the raw Postgres type string.
        class AllJsonDialect(dl.PostgresDialect):
            name = "stub-json"

            def physical_token(self, data_type, udt):
                return dl.PT_JSON

        col = {"name": "amount", "data_type": "numeric", "udt": "numeric",
               "numeric_precision": 18, "numeric_scale": 2}
        sem_pg, *_ = ont.infer_property(col, is_pk=False, is_fk=False, enum_udts=set(),
                                        dialect=dl.PostgresDialect())
        sem_stub, *_ = ont.infer_property(col, is_pk=False, is_fk=False, enum_udts=set(),
                                          dialect=AllJsonDialect())
        self.assertEqual(sem_pg, ont.SEM_CURRENCY_DECIMAL)
        self.assertEqual(sem_stub, ont.SEM_SEMI_STRUCTURED)

    @unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
    def test_lift_default_equals_explicit_postgres(self):
        profile = json.loads(PROFILE.read_text())
        default = ont.lift(profile).to_json()
        explicit = ont.lift(profile, dialect=dl.PostgresDialect()).to_json()
        self.assertEqual(default, explicit)

    def test_canonicalize_delegates_float_epsilon_to_dialect(self):
        # narrow float (float4) → 6 sig digits; wide → 12. Driven by the dialect token.
        v = "0.1234567"
        narrow = rc.canonicalize(mp.RC_APPROX_FLOAT, v, source_udt="float4")
        wide = rc.canonicalize(mp.RC_APPROX_FLOAT, v, source_udt="float8")
        self.assertNotEqual(narrow, wide)
        self.assertEqual(narrow, f"{float(v):.6g}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
