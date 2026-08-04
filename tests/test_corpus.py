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
               entity: str = "t", column: str = "c",
               dialect: dl.SourceDialect | None = None,
               facts: dict[str, object] | None = None) -> cp.ColumnAudit:
    """Audit a single column against a scripted seed and a scripted read-back."""
    seed = Path(_TMP.name) / "seed.sql"
    seed.write_text(seed_sql)
    hard = [{"table": entity, "column": column}]
    maps = [_mapping(entity, column, reconcile)]
    audits = cp.audit_corpus(hard, seed, maps,
                             {(entity, column): (facts or {}).get("udt", "")},
                             dialect=dialect or _StubDialect(stored),
                             col_facts={(entity, column): facts} if facts else None)
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

    def test_engine_added_host_mask_is_not_drift(self):
        """SAU-516 decision 1: `inet` omits the mask; the type stores `/32`.

        Equal under the type's own equality operator, round-trip stable, nothing lost —
        so filing it beside SAU-503's irreversible rounding would cost the real findings
        their credibility. Reconciled, and the rule that did it is named.
        """
        a = _audit_one("INSERT INTO t (c) VALUES ('203.0.113.42'), ('::1');",
                       [{"c": "203.0.113.42/32"}, {"c": "::1/128"}],
                       facts={"data_type": "inet", "udt": "inet"})
        self.assertEqual(a.verdict, cp.V_VERIFIED, a.to_json())
        self.assertEqual([e["rule"] for e in a.equivalences],
                         [dl.EQ_INET_HOST_MASK] * 2)
        # both raw forms survive into the record — an equivalence that hid its own
        # application would be the invisible-normalization defect one layer up.
        self.assertEqual(a.equivalences[0],
                         {"claimed": "203.0.113.42", "stored": "203.0.113.42/32",
                          "rule": dl.EQ_INET_HOST_MASK})

    def test_narrower_mask_widened_to_host_mask_is_still_drift(self):
        """The inverse test the ruling requires: `/24 → /32` must stay RED.

        This is the boundary that separates a documented type equivalence from a
        blanket strip-the-mask rule. `10.0.0.0/24` is a network of 256 addresses;
        `/32` is one host. Reconciling those is real data loss, and a rule broad enough
        to hide it would trade a genuine hole in gate coverage for cosmetics. The test
        exists so the rule cannot be widened without something failing.
        """
        a = _audit_one("INSERT INTO t (c) VALUES ('10.0.0.0/24');",
                       [{"c": "10.0.0.0/32"}],
                       facts={"data_type": "inet", "udt": "inet"})
        self.assertEqual(a.verdict, cp.V_DRIFTED, a.to_json())
        self.assertEqual(a.missing, ["10.0.0.0/24"])
        self.assertEqual(a.unclaimed, ["10.0.0.0/32"])
        self.assertEqual(a.equivalences, [])

    def test_equivalence_does_not_leak_to_other_types(self):
        # The rule is scoped by the dialect's own physical-type token, so a text column
        # holding an address-shaped string gets no such courtesy.
        a = _audit_one("INSERT INTO t (c) VALUES ('203.0.113.42');",
                       [{"c": "203.0.113.42/32"}],
                       facts={"data_type": "text", "udt": "text"})
        self.assertEqual(a.verdict, cp.V_DRIFTED, a.to_json())

    def test_equivalence_is_opt_in_per_lane(self):
        # The base dialect declares none: an equivalence nobody wrote down is
        # indistinguishable from silently accepting drift.
        self.assertIsNone(dl.SourceDialect.claim_equivalence(
            dl.SqlServerDialect(), "203.0.113.42", "203.0.113.42/32",
            source_udt="inet", data_type="inet"))

    def test_unreadable_seed_dialect_fails_closed(self):
        class _NoGrammar(_StubDialect):
            sql_read = ""
        with self.assertRaises(ValueError):
            cp.audit_corpus([{"table": "t", "column": "c"}], SEED,
                            [_mapping("t", "c", mp.RC_EXACT_TEXT)], {},
                            dialect=_NoGrammar([]))


# ── The third state: not-seed-declarable (SAU-516 decision 2) ───────────────

class _StubSqlServer(dl.SqlServerDialect):
    """SQL Server lane with a scripted read-back."""

    def __init__(self, rows: list[dict[str, object]]):
        self._rows = rows

    def extract_rows(self, entity, mappings):  # noqa: D102
        return self._rows


class TestNotSeedDeclarable(unittest.TestCase):
    """The exemption must be derived from the type, and unabusable by construction.

    Permanent RED for a structural reason is a gate the team routes around within two
    sprints, and then it protects nothing. A hand-written exemption list is an answer key
    by another name — the day someone appends a normal column to it, a genuinely
    unplanted trap goes silent. The catalog is the third option: engine-generated is a
    property of the *type*, so the only way to widen the exemption is to misdeclare one.
    """

    SEED = "INSERT INTO t (other) VALUES ('x');"

    def _audit(self, facts):
        return _audit_one(self.SEED, [{"c": "0x0000000000000007"}],
                          dialect=_StubSqlServer([{"c": "0x0000000000000007"}]),
                          facts=facts)

    def test_rowversion_is_a_third_state_not_a_failure(self):
        a = self._audit({"data_type": "timestamp", "udt": "timestamp"})
        self.assertEqual(a.verdict, cp.V_NOT_DECLARABLE, a.to_json())
        self.assertIn("no INSERT can name this column", a.not_declarable or "")

    def test_third_state_is_not_a_pass(self):
        # The distinction that keeps the readout honest: exempt is neither verified nor
        # drifted, and must never be added to the verified total.
        a = self._audit({"data_type": "timestamp", "udt": "timestamp"})
        self.assertNotEqual(a.verdict, cp.V_VERIFIED)
        self.assertNotEqual(a.verdict, cp.V_DRIFTED)

    def test_a_normal_column_the_seed_forgot_still_fails(self):
        # The whole load-bearing property. A plain VARCHAR is seed-declarable, so the
        # seed forgetting it is exactly the SAU-503 shape and must stay RED.
        a = self._audit({"data_type": "varchar", "udt": "varchar"})
        self.assertEqual(a.verdict, cp.V_DRIFTED, a.to_json())
        self.assertIn("claims nothing to verify", a.error or "")

    def test_a_default_is_not_an_exemption(self):
        """A DEFAULT-ed column is one an INSERT *may* name and this seed did not.

        Exempting it would silence a trap by adding a DEFAULT to the schema — the
        list-that-rots failure mode with extra steps. Row-level DEFAULTs are already
        handled honestly one level down, as an announced weakening of the comparison.
        """
        a = self._audit({"data_type": "varchar", "udt": "varchar", "has_default": True})
        self.assertEqual(a.verdict, cp.V_DRIFTED, a.to_json())

    def test_identity_is_not_an_exemption_because_it_is_declarable(self):
        # SET IDENTITY_INSERT lets an INSERT name it, and this lane's seed does exactly
        # that — so IDENTITY is a fact about the column, not a licence to skip it.
        a = self._audit({"data_type": "int", "udt": "int", "is_identity": True})
        self.assertEqual(a.verdict, cp.V_DRIFTED, a.to_json())

    def test_computed_columns_are_the_general_category(self):
        # Not a ROWVERSION carve-out: the base dialect exempts computed columns for
        # every lane, because no INSERT may supply their value in any of them.
        a = _audit_one(self.SEED, [{"c": "x"}], facts={"data_type": "text",
                                                       "is_computed": True})
        self.assertEqual(a.verdict, cp.V_NOT_DECLARABLE, a.to_json())

    def test_unknown_facts_fail_closed(self):
        # No catalog facts at all → treated as declarable, i.e. still a failure. An
        # exemption by omission is the one that would never be noticed.
        a = _audit_one(self.SEED, [{"c": "x"}])
        self.assertEqual(a.verdict, cp.V_DRIFTED, a.to_json())

    def test_a_seeded_rowversion_would_still_be_verified(self):
        # The exemption only applies where the seed states no claim. If an INSERT does
        # name the column, bullet 0 checks it like any other — the third state is about
        # a missing claim, never about skipping a present one.
        a = _audit_one("INSERT INTO t (c) VALUES ('0x01');", [{"c": "0x02"}],
                       dialect=_StubSqlServer([{"c": "0x02"}]),
                       facts={"data_type": "timestamp", "udt": "timestamp"})
        self.assertEqual(a.verdict, cp.V_DRIFTED, a.to_json())
        self.assertEqual(a.missing, ["0x01"])


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

    def _skeleton(self, b0: dict) -> gate_mod.GateResult:
        g = gate_mod.GateResult()
        g.bullets["0_corpus_validity"] = b0
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
        return g

    def _readout(self, b0: dict) -> str:
        return gate_mod.write_readout(self._skeleton(b0),
                                      Path(_TMP.name) / "readout.md")

    def test_readout_names_the_column_and_both_values(self):
        text = self._readout({
            "pass": False, "seed": "seed.sql", "audit_error": None,
            "columns_audited": 2, "columns_verified": 1, "columns_drifted": 1,
            "columns_not_seed_declarable": 0, "method": "m",
            "drifted": [{"column": "audit_log.ip_address", "comparison": "multiset",
                         "error": None, "claimed_but_not_stored": ["203.0.113.42"],
                         "stored_but_not_claimed": ["203.0.113.42/32"]}],
            "detail": [],
        })
        self.assertIn("audit_log.ip_address", text)
        self.assertIn("203.0.113.42", text)          # claimed
        self.assertIn("203.0.113.42/32", text)       # actually stored
        self.assertIn("NO-GO", text)

    def test_readout_prints_raw_forms_and_names_the_rule(self):
        """SAU-516 constraint 3: an equivalence that hides its own application IS the
        invisible-normalization defect, one layer up. Auditability is the entire
        difference between declaring a type equivalence and editing the seed."""
        text = self._readout({
            "pass": True, "seed": "seed.sql", "audit_error": None,
            "columns_audited": 1, "columns_verified": 1, "columns_drifted": 0,
            "columns_not_seed_declarable": 0, "method": "m", "drifted": [],
            "equivalences": [{"column": "audit_log.ip_address",
                              "claimed": "203.0.113.42",
                              "stored": "203.0.113.42/32",
                              "rule": dl.EQ_INET_HOST_MASK}],
            "detail": [],
        })
        self.assertIn("203.0.113.42", text)          # raw claimed
        self.assertIn("203.0.113.42/32", text)       # raw stored
        self.assertIn(dl.EQ_INET_HOST_MASK, text)    # …and the rule that reconciled them

    def test_readout_never_folds_the_exempt_column_into_the_pass_total(self):
        """A gate that reports 20/20 when one column was exempt is lying. Three
        columns, always — an honest three-column count over a clean two-column one."""
        text = self._readout({
            "pass": True, "seed": "seed.sql", "audit_error": None,
            "columns_audited": 20, "columns_verified": 19, "columns_drifted": 0,
            "columns_not_seed_declarable": 1, "method": "m", "drifted": [],
            "not_seed_declarable": [{"column": "audit_trail.row_version",
                                     "not_seed_declarable": "engine-generated"}],
            "detail": [],
        })
        self.assertIn("19 verified", text)
        self.assertIn("1 not-seed-declarable", text)
        self.assertIn("0 drifted", text)
        self.assertNotIn("20/20", text)
        self.assertIn("audit_trail.row_version", text)


# ── The committed Postgres lane, audited for real ───────────────────────────

@unittest.skipUnless(_pg_reachable(), "no Postgres reachable (set libpq env vars)")
class TestCommittedPostgresLane(unittest.TestCase):
    """Pins bullet 0's verdict on the shipped lane, column by column.

    A *characterization* test: it records what the audit finds, so nothing can move
    under us silently. Since SAU-516 the whole lane is expected to verify — the one
    finding it carried (`audit_log.ip_address`) was ruled a documented type
    equivalence, not drift. The equivalence assertions below are the load-bearing half:
    they pin that the column is reconciled BY A NAMED RULE with both raw forms
    recorded, not by the seed having been quietly edited to say `/32`.
    """

    def setUp(self):
        onto = ont.lift(ont.load_profile(PROFILE))
        self.maps = mp.map_ontology(onto)
        self.udt = {(e.name, p.name): p.udt for e in onto.entities for p in e.properties}
        self.facts = {(e.name, p.name): {"data_type": p.data_type, "udt": p.udt,
                                         "is_identity": p.is_identity,
                                         "is_computed": p.is_computed,
                                         "has_default": p.has_default}
                      for e in onto.entities for p in e.properties}
        self.hard = json.loads(MANIFEST.read_text())["hard_columns"]

    def _audits(self):
        return cp.audit_corpus(self.hard, SEED, self.maps, self.udt,
                               col_facts=self.facts)

    def test_every_ground_truth_column_verifies(self):
        audits = self._audits()
        self.assertEqual(len(audits), len(self.hard))
        self.assertEqual({a.verdict for a in audits}, {cp.V_VERIFIED},
                         [a.to_json() for a in audits if a.verdict != cp.V_VERIFIED])

    def test_the_inet_column_is_reconciled_by_the_named_rule_not_by_a_seed_edit(self):
        a = next(x for x in self._audits()
                 if f"{x.entity}.{x.column}" == "audit_log.ip_address")
        self.assertEqual({e["rule"] for e in a.equivalences}, {dl.EQ_INET_HOST_MASK})
        # the seed still claims the bare address, and the engine still stores the mask —
        # both facts are on the record, which is what keeps the rule auditable
        self.assertIn(("203.0.113.42", "203.0.113.42/32"),
                      {(e["claimed"], e["stored"]) for e in a.equivalences})

    def test_the_seed_was_not_edited_to_match_the_engine(self):
        """The rejected option (a), pinned so it cannot arrive later by accident.

        Normalizing the seed literal to `/32` would transcribe the engine's output back
        into the claim — destroying the seed's independence, which is bullet 0's only
        load-bearing property, and leaving a tautology that reports GREEN.
        """
        claims = cp.seed_claims(SEED, read="postgres")
        vals = [cp._literal_text(v) for v in claims[("audit_log", "ip_address")]]
        self.assertEqual(vals, ["203.0.113.42", "198.51.100.7", "::1",
                                "2001:db8::8a2e:370:7334"])
        for v in vals:
            self.assertNotIn("/", v, "the seed must state the bare address it always did")


if __name__ == "__main__":
    unittest.main(verbosity=2)
