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

    def test_network_extraction_is_the_canonical_text_cast_not_a_mask_strip(self):
        """ADR-0001 pinned at the *extraction* expression (SAU-518).

        SAU-516 pinned the equivalence RULE; nothing pinned the expression that produces
        the value the rule is applied to. `inet::text` is the type's canonical output
        form and keeps the mask (`203.0.113.42/32`, `10.0.0.0/24`). `host()` and
        `abbrev()` both drop it — `host(inet '10.0.0.0/24')` is `10.0.0.0`, a different
        network — and ADR-0001 ruled the strip is the irreversible direction.

        This is the DB-free half. The half that actually watches the wire is
        `test_corpus.TestCommittedPostgresLane`, and the corpus tripwire is the
        `10.0.0.0/24` seed row: with only host addresses seeded, a mask-stripping
        extraction matches every bare literal exactly and the gate goes *greener*.
        """
        expr = self.d.extraction_expr("ip_address", mp.RC_EXACT_TEXT)
        self.assertEqual(expr, '"ip_address"::text')
        for stripper in ("host(", "abbrev(", "masklen(", "network(", "set_masklen("):
            self.assertNotIn(stripper, expr.lower(),
                             f"{stripper} discards the mask ADR-0001 ruled canonical")


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

    def test_inet_host_mask_equivalence_and_its_boundary(self):
        """Seam 5 (SAU-516): the `inet` equivalence, pinned at both edges.

        The rule passes the test that separates it from an answer key — "an omitted mask
        means the full host mask" is documented PostgreSQL semantics, written the same
        way whether or not we had ever seen this corpus. The negative cases are the
        load-bearing half: a rule broad enough to also swallow `/24 → /32` would trade a
        real hole in gate coverage for cosmetics.
        """
        d = dl.PostgresDialect()
        eq = lambda c, s: d.claim_equivalence(c, s, source_udt="inet", data_type="inet")
        # in scope: the claim omits the mask, storage adds exactly the host mask
        self.assertEqual(eq("203.0.113.42", "203.0.113.42/32"), dl.EQ_INET_HOST_MASK)
        self.assertEqual(eq("::1", "::1/128"), dl.EQ_INET_HOST_MASK)
        self.assertEqual(eq("2001:db8::8a2e:370:7334", "2001:db8::8a2e:370:7334/128"),
                         dl.EQ_INET_HOST_MASK)
        # out of scope — every one of these stays a finding
        self.assertIsNone(eq("10.0.0.0/24", "10.0.0.0/32"))   # narrower net → one host
        self.assertIsNone(eq("10.0.0.0/24", "10.0.0.0"))      # mask dropped entirely
        self.assertIsNone(eq("203.0.113.42", "203.0.113.42/24"))   # not the host mask
        self.assertIsNone(eq("203.0.113.42", "203.0.113.99/32"))   # different address
        self.assertIsNone(eq("::1", "::1/32"))                # v4 host mask on a v6 addr
        self.assertIsNone(eq("203.0.113.42", "203.0.113.42/128"))  # v6 mask on a v4 addr
        self.assertIsNone(eq(None, "203.0.113.42/32"))
        self.assertIsNone(eq("203.0.113.42", None))
        # and it is scoped by the dialect's own physical-type token, not by shape
        self.assertIsNone(d.claim_equivalence("203.0.113.42", "203.0.113.42/32",
                                              source_udt="text", data_type="text"))

    def test_canonicalize_delegates_float_epsilon_to_dialect(self):
        # narrow float (float4) → 6 sig digits; wide → 12. Driven by the dialect token.
        v = "0.1234567"
        narrow = rc.canonicalize(mp.RC_APPROX_FLOAT, v, source_udt="float4")
        wide = rc.canonicalize(mp.RC_APPROX_FLOAT, v, source_udt="float8")
        self.assertNotEqual(narrow, wide)
        self.assertEqual(narrow, f"{float(v):.6g}")


class TestPostgresTypeRulesSau549(unittest.TestCase):
    """SAU-549: uuid / bytea / interval / time / money / xml each get a type-pinned rule
    instead of the `unrecognized_type` VARCHAR@0.5 fallback."""

    def setUp(self):
        self.d = dl.PostgresDialect()

    def _map(self, data_type, udt, **extra):
        col = {"name": "c", "ordinal": 1, "data_type": data_type, "udt": udt, **extra}
        onto = ont.lift({"tables": [{"table": "t", "columns": [col]}]}, dialect=self.d)
        return mp.map_ontology(onto)[0]

    def test_tokens(self):
        tok = self.d.physical_token
        self.assertEqual(tok("uuid", "uuid"), dl.PT_UUID)
        self.assertEqual(tok("bytea", "bytea"), dl.PT_BINARY)
        self.assertEqual(tok("money", "money"), dl.PT_MONEY)
        self.assertEqual(tok("time without time zone", "time"), dl.PT_TIME)
        self.assertEqual(tok("interval", "interval"), dl.PT_INTERVAL)
        self.assertEqual(tok("xml", "xml"), dl.PT_XML)
        # timetz would lose its offset in a TIME target — stays unrecognized
        self.assertEqual(tok("time with time zone", "timetz"), dl.PT_UNKNOWN)

    def test_targets_and_confidence(self):
        cases = {
            ("uuid", "uuid"): ("VARCHAR(36)", 0.95),
            ("bytea", "bytea"): ("BINARY", 0.9),          # unbounded — never BINARY(8)
            ("money", "money"): ("NUMBER(19,2)", 0.98),   # int8 minor units, fixed scale
            ("time without time zone", "time"): ("TIME", 0.95),
            ("interval", "interval"): ("VARCHAR(16777216)", 0.85),
            ("xml", "xml"): ("VARCHAR(16777216)", 0.85),
        }
        for (dt, udt), (target, conf) in cases.items():
            m = self._map(dt, udt)
            self.assertEqual((m.snowflake_type, m.confidence), (target, conf), dt)
        self.assertEqual(self._map("time without time zone", "time").reconcile, mp.RC_TIME)

    def test_implicit_shape_never_overrides_the_catalog(self):
        m = self._map("money", "money", numeric_precision=10, numeric_scale=4)
        self.assertEqual(m.snowflake_type, "NUMBER(10,4)")

    def test_extraction_wire_forms(self):
        self.assertEqual(self.d.extraction_expr("amount", mp.RC_EXACT_TEXT, "money(19,2)"),
                         '"amount"::numeric::text')
        self.assertEqual(self.d.extraction_expr("content", mp.RC_EXACT_TEXT, "bytea"),
                         "encode(\"content\", 'hex')")
        self.assertEqual(self.d.extraction_expr("x", mp.RC_EXACT_TEXT, "uuid"), '"x"::text')

    def test_time_canonicalization(self):
        from datetime import time
        c = lambda v: rc.canonicalize(mp.RC_TIME, v)
        self.assertEqual(c("08:30:00.5"), c(time(8, 30, 0, 500000)))
        self.assertEqual(c("23:59:59.999999"), c(time(23, 59, 59, 999999)))
        self.assertNotEqual(c("23:59:59.999999"), c(time(23, 59, 59)))   # no rollover/trim
        self.assertIsNone(c(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
