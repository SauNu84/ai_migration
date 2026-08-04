#!/usr/bin/env python3
"""SAU-513 — §8 gate bullet 0: corpus validity.

Bullet 0 asks a question the other four bullets structurally cannot: *did the ground
truth we claim to have seeded actually land in the source?* Reconciliation compares
source-as-read to target-as-read, and both anchors sit downstream of anything the source
engine normalized at INSERT — so a trap the engine quietly rewrote is invisible to it.

The tests that matter here are the ones that **corrupt something and prove the assertion
fires**. An assertion only ever observed passing is the same class of evidence as a trap
that was never planted, which is the defect this whole bullet exists to catch. So each
failure mode gets a test that deliberately breaks it:

  * a seed literal that the source does not hold  (the plain corrupted-seed case)
  * a value the engine silently *rounded*         (the SAU-503 Oracle INTERVAL shape)
  * a value the engine silently *nulled*          (the SAU-499 Oracle '' → NULL shape)
  * scale dropped inside an array                 (bullet 0 must not inherit the
                                                   float coercion in `_canon_json`)
  * a seed literal the parser cannot read         (must raise, never score as agreement)

Run:
    PGHOST=/tmp PGPORT=5599 PGUSER=postgres PGDATABASE=phase0 \
        python3 -m unittest tests.test_corpus -v
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

import sqlglot

from core import corpus as cp
from core import dialect as dl
from core import gate as gate_mod
from core import mapper as mp
from core import ontology as ont

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "artifacts" / "profile.json"
MANIFEST = ROOT / "ground_truth" / "hard_columns.json"
SEED = ROOT / "schema" / "02_seed.sql"


def _pg_reachable() -> bool:
    try:
        p = subprocess.run(["psql", "-X", "-tA", "-c", "SELECT 1"],
                           capture_output=True, text=True, timeout=5)
        return p.returncode == 0 and p.stdout.strip() == "1"
    except Exception:
        return False


class _StubDialect(dl.PostgresDialect):
    """A source whose read-back is scripted, so 'what the engine stored' is controllable."""

    def __init__(self, rows: list[dict[str, object]]):
        self._rows = rows

    def extract_rows(self, entity, mappings):  # noqa: D102 — see class docstring
        return self._rows


def _mapping(entity: str, column: str, reconcile: str) -> mp.Mapping:
    return mp.Mapping(entity=entity, column=column, pg_type="text",
                      semantic_type=ont.SEM_AMBIGUOUS_TEXT, snowflake_type="VARCHAR",
                      reconcile=reconcile, confidence=1.0, governance_tags=[], flags=[],
                      notes="")


def _audit_one(seed_sql: str, stored: list[dict[str, object]], *,
               reconcile: str = mp.RC_EXACT_TEXT,
               entity: str = "t", column: str = "c") -> cp.ColumnAudit:
    """Audit a single column against a scripted seed and a scripted read-back."""
    seed = Path(_TMP.name) / "seed.sql"
    seed.write_text(seed_sql)
    hard = [{"table": entity, "column": column}]
    maps = [_mapping(entity, column, reconcile)]
    audits = cp.audit_corpus(hard, seed, maps, {}, dialect=_StubDialect(stored))
    return audits[0]


_TMP: "tempfile.TemporaryDirectory"


def setUpModule():
    global _TMP
    import tempfile
    _TMP = tempfile.TemporaryDirectory()


def tearDownModule():
    _TMP.cleanup()


# ── The claim side: rendering seed literals ─────────────────────────────────

class TestSeedLiterals(unittest.TestCase):
    def _lit(self, sql: str, read: str = "postgres"):
        return cp._literal_text(sqlglot.parse_one(f"SELECT {sql}", read=read).selects[0])

    def test_literal_shapes_render_to_their_wire_form(self):
        # Type prefixes and quotes are SQL syntax, not part of the claimed value.
        self.assertEqual(self._lit("'0042'"), "0042")
        self.assertEqual(self._lit("TIMESTAMPTZ '2024-03-01 08:15:00+00'"),
                         "2024-03-01 08:15:00+00")
        self.assertEqual(self._lit("DATE '2024-01-31'"), "2024-01-31")
        self.assertEqual(self._lit("NULL"), None)
        self.assertEqual(self._lit("TRUE"), "true")
        self.assertEqual(self._lit("-1.5"), "-1.5")
        self.assertEqual(self._lit("N'Alice'", read="tsql"), "Alice")

    def test_numeric_array_elements_keep_their_scale(self):
        # 10.00 must not become 10.0 — the scale is the thing under test.
        self.assertEqual(self._lit("ARRAY[10.00, 5.50, 2.25]"), "[10.00,5.50,2.25]")

    def test_postgres_array_literal_string_form(self):
        self.assertEqual(cp.claimed_text(
            sqlglot.parse_one("SELECT '{}'").selects[0], mp.RC_ARRAY_CANONICAL), "[]")
        self.assertEqual(cp.claimed_text(
            sqlglot.parse_one("SELECT '{priority,gift}'").selects[0],
            mp.RC_ARRAY_CANONICAL), '["priority","gift"]')

    def test_unreadable_literal_raises_rather_than_agreeing(self):
        # A literal the renderer cannot read must never be scored as a match.
        with self.assertRaises(ValueError):
            self._lit("now()")
        with self.assertRaises(ValueError):
            cp._pg_array_literal('{"quoted,elem"}')

    def test_seed_claims_reads_the_committed_postgres_seed(self):
        claims = cp.seed_claims(SEED, read="postgres")
        vals = [cp._literal_text(v) for v in claims[("app_user", "ssn_last4")]]
        self.assertEqual(vals, ["0042", "0007", None, "1000", "0815"])
        # dbo./public. qualifiers are stripped so manifest table names match
        self.assertIn(("org", "created_at"), claims)


# ── The assertion fires: each failure mode, deliberately induced ────────────

class TestAuditCatchesDrift(unittest.TestCase):
    def test_intact_corpus_passes(self):
        a = _audit_one("INSERT INTO t (c) VALUES ('0042'), ('0007');",
                       [{"c": "0042"}, {"c": "0007"}])
        self.assertTrue(a.ok, a.to_json())

    def test_corrupted_seed_value_is_caught(self):
        """Acceptance criterion 3: the seed claims a value the source does not hold."""
        a = _audit_one("INSERT INTO t (c) VALUES ('0042'), ('0007');",
                       [{"c": "9999"}, {"c": "0007"}])
        self.assertFalse(a.ok)
        self.assertEqual(a.missing, ["0042"])       # named the claimed literal…
        self.assertEqual(a.unclaimed, ["9999"])     # …and what the engine actually stored

    def test_silent_rounding_is_caught(self):
        """The SAU-503 shape: the engine rounds at INSERT, corpus still looks populated."""
        a = _audit_one("INSERT INTO t (c) VALUES ('12:34:56.123456789');",
                       [{"c": "12:34:56.123457000"}])
        self.assertFalse(a.ok)
        self.assertEqual(a.missing, ["12:34:56.123456789"])
        self.assertEqual(a.unclaimed, ["12:34:56.123457000"])

    def test_silent_empty_string_to_null_is_caught(self):
        """The SAU-499 shape: '' collapses to NULL before the chassis ever reads it.

        Reconciliation cannot see this — source-as-read and target-as-read agree on NULL,
        so the gate goes GREEN while the trap tests nothing.
        """
        a = _audit_one("INSERT INTO t (c) VALUES ('');", [{"c": None}])
        self.assertFalse(a.ok)
        self.assertEqual(a.missing, [""])
        self.assertEqual(a.unclaimed, [None])

    def test_array_scale_loss_is_caught(self):
        """Bullet 0 must not inherit reconcile's float coercion.

        `reconcile._canon_json` parses JSON through Python floats, so 10.00 → 10.0. Reusing
        it here would make bullet 0 blind to precisely the precision loss it exists to
        catch, so structs are canonicalized through Decimal instead.
        """
        a = _audit_one("INSERT INTO t (c) VALUES (ARRAY[10.00, 5.50]);",
                       [{"c": "[10.0, 5.5]"}], reconcile=mp.RC_ARRAY_CANONICAL)
        self.assertFalse(a.ok)
        self.assertEqual(a.missing, ["[10.00,5.50]"])

    def test_semantically_equal_json_does_not_false_alarm(self):
        # Key order/whitespace are not claims the seed makes; drift must be real drift.
        a = _audit_one("""INSERT INTO t (c) VALUES ('{"b":1,"a":2}');""",
                       [{"c": '{"a": 2, "b": 1}'}], reconcile=mp.RC_JSON_CANONICAL)
        self.assertTrue(a.ok, a.to_json())

    def test_column_the_seed_never_names_is_a_failure_not_a_skip(self):
        # The manifest claims a trap lives here; nothing planting it is the SAU-503 shape.
        a = _audit_one("INSERT INTO t (other) VALUES ('x');", [{"c": "x"}])
        self.assertFalse(a.ok)
        self.assertIn("claims nothing to verify", a.error or "")

    def test_defaulted_rows_weaken_the_check_and_say_so(self):
        # A row that took a DEFAULT makes counts non-comparable — announce it, don't hide it.
        a = _audit_one("INSERT INTO t (c) VALUES ('a'); INSERT INTO t (other) VALUES ('z');",
                       [{"c": "a"}, {"c": "server-default"}])
        self.assertTrue(a.ok, a.to_json())
        self.assertIn("DEFAULT", a.comparison)

    def test_unreadable_seed_dialect_fails_closed(self):
        class _NoGrammar(_StubDialect):
            sql_read = ""
        with self.assertRaises(ValueError):
            cp.audit_corpus([{"table": "t", "column": "c"}], SEED,
                            [_mapping("t", "c", mp.RC_EXACT_TEXT)], {},
                            dialect=_NoGrammar([]))


# ── Gate wiring ─────────────────────────────────────────────────────────────

class TestGateWiring(unittest.TestCase):
    def test_bullet_zero_runs_before_bullet_one(self):
        self.assertEqual(list(gate_mod.GateResult().bullets), [])
        order = ["0_corpus_validity", "1_reconciliation"]
        self.assertEqual(sorted(order), order, "bullet 0 must sort/run ahead of bullet 1")

    def test_failed_bullet_zero_forces_gate_red(self):
        g = gate_mod.GateResult()
        g.bullets["0_corpus_validity"] = {"pass": False}
        for k in ("1_reconciliation", "2_byte_identical_regen", "3_confidence_calibration",
                  "4_hard_fail_detection"):
            g.bullets[k] = {"pass": True}
        self.assertFalse(g.passed, "a corpus that did not land must make the gate RED")

    def test_readout_names_the_column_and_both_values(self):
        g = gate_mod.GateResult()
        g.bullets["0_corpus_validity"] = {
            "pass": False, "seed": "seed.sql", "audit_error": None,
            "columns_audited": 2, "columns_intact": 1, "method": "m",
            "drifted": [{"column": "audit_log.ip_address", "comparison": "multiset",
                         "error": None, "claimed_but_not_stored": ["203.0.113.42"],
                         "stored_but_not_claimed": ["203.0.113.42/32"]}],
            "detail": [],
        }
        for k, extra in (("1_reconciliation",
                          {"consecutive_clean_runs": 0, "required": 3,
                           "snowflake_compile_errors": [], "runs": []}),
                         ("2_byte_identical_regen",
                          {"iterations": 1, "distinct_digests": 1, "sha256": "x",
                           "method": "m"}),
                         ("3_confidence_calibration",
                          {"target_correct": 0, "hard_columns": 0,
                           "meets_confidence_floor": 0, "detail": []}),
                         ("4_hard_fail_detection",
                          {"traps_caught": 0, "traps_injected": 0,
                           "false_alarms_on_correct_mapping": [], "detail": []})):
            g.bullets[k] = {"pass": True, **extra}
        out = Path(_TMP.name) / "readout.md"
        text = gate_mod.write_readout(g, out)
        self.assertIn("audit_log.ip_address", text)
        self.assertIn("203.0.113.42", text)          # claimed
        self.assertIn("203.0.113.42/32", text)       # actually stored
        self.assertIn("NO-GO", text)


# ── The committed Postgres lane, audited for real ───────────────────────────

@unittest.skipUnless(_pg_reachable(), "no Postgres reachable (set libpq env vars)")
class TestCommittedPostgresLane(unittest.TestCase):
    """Pins bullet 0's finding on the shipped lane.

    This is a *characterization* test: it records what the audit currently finds, which
    today is one drifted column. It is not an assertion that the drift is acceptable —
    that call is the COO's (SAU-513 puts corpus fixes explicitly out of scope). The test
    exists so the finding cannot be silently re-seeded away, and so any *new* drift, or
    any change to this one, fails loudly instead of moving under us.
    """

    KNOWN_DRIFT = {"audit_log.ip_address"}

    def setUp(self):
        onto = ont.lift(ont.load_profile(PROFILE))
        self.maps = mp.map_ontology(onto)
        self.udt = {(e.name, p.name): p.udt for e in onto.entities for p in e.properties}
        self.hard = json.loads(MANIFEST.read_text())["hard_columns"]

    def test_known_drift_set_is_exactly_as_reported(self):
        audits = cp.audit_corpus(self.hard, SEED, self.maps, self.udt)
        self.assertEqual(len(audits), len(self.hard))
        drifted = {f"{a.entity}.{a.column}" for a in audits if not a.ok}
        self.assertEqual(drifted, self.KNOWN_DRIFT)

    def test_inet_drift_is_a_netmask_the_engine_added(self):
        audits = cp.audit_corpus(self.hard, SEED, self.maps, self.udt)
        a = next(x for x in audits if f"{x.entity}.{x.column}" == "audit_log.ip_address")
        self.assertIn("203.0.113.42", a.missing)
        self.assertIn("203.0.113.42/32", a.unclaimed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
