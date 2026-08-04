#!/usr/bin/env python3
"""SAU-305 core-loop acceptance tests — the §8 exit gate.

Covers the full lift → map → codegen → verification loop and asserts each §8
acceptance bullet. Tests split into two tiers:

  * **DB-free** (always run): lift correctness, mapping of every hard column to its
    expected Snowflake target ≥ its confidence floor, Snowflake compile-check,
    byte-identical SQL regeneration, and that the semantic guard catches every
    documented trap with zero false alarms on the correct mapping.
  * **DB-backed** (skip if no Postgres reachable): 3/3 consecutive clean
    reconciliation runs on Gold, and that the ablation/naive mapper is caught.

Run:
    PGHOST=/tmp PGPORT=5599 PGUSER=postgres python3 -m unittest tests.test_core_loop -v
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from core import ontology as ont
from core import mapper as mp
from core import codegen as cg
from core import reconcile as rc
from core import gate as gate_mod

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "artifacts" / "profile.json"
MANIFEST = ROOT / "ground_truth" / "hard_columns.json"


def _pg_reachable() -> bool:
    try:
        p = subprocess.run(["psql", "-X", "-tA", "-c", "SELECT 1"],
                           capture_output=True, text=True, timeout=5)
        return p.returncode == 0 and p.stdout.strip() == "1"
    except Exception:
        return False


def _load():
    profile = json.loads(PROFILE.read_text())
    manifest = json.loads(MANIFEST.read_text())
    onto = ont.lift(profile)
    maps = mp.map_ontology(onto)
    return profile, manifest, onto, maps


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestLiftAndMap(unittest.TestCase):
    def setUp(self):
        self.profile, self.manifest, self.onto, self.maps = _load()
        self.map_index = {(m.entity, m.column): m for m in self.maps}

    def test_ontology_valid_and_complete(self):
        self.assertFalse(ont.validate(self.onto))
        self.assertGreaterEqual(len(self.onto.entities), 15)
        self.assertTrue(self.onto.relationships)

    def test_hard_columns_map_to_expected_target_at_floor(self):
        for h in self.manifest["hard_columns"]:
            m = self.map_index[(h["table"], h["column"])]
            self.assertTrue(
                gate_mod._target_compatible(m.snowflake_type, h["snowflake_target"]),
                f"{h['table']}.{h['column']}: got {m.snowflake_type}, "
                f"expected {h['snowflake_target']}",
            )
            self.assertGreaterEqual(
                m.confidence, h["confidence_floor"],
                f"{h['table']}.{h['column']}: confidence {m.confidence} "
                f"< floor {h['confidence_floor']}",
            )

    def test_tz_distinction_preserved(self):
        self.assertEqual(self.map_index[("org", "created_at")].snowflake_type, "TIMESTAMP_TZ")
        self.assertEqual(self.map_index[("app_user", "last_login_at")].snowflake_type, "TIMESTAMP_NTZ")

    def test_pii_governance_tagged(self):
        for key in (("app_user", "ssn_last4"), ("app_user", "email"), ("payment", "card_last4")):
            self.assertIn("PII", self.map_index[key].governance_tags, key)


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestCodegen(unittest.TestCase):
    def setUp(self):
        _, _, self.onto, self.maps = _load()
        self.code = cg.generate(self.maps)

    def test_snowflake_compiles(self):
        self.assertEqual(cg.compile_check(self.code), [])

    def test_target_compatible_enforces_varchar_width(self):
        # CTO gate-hardening: an explicit manifest width must match exactly; only an
        # unwidthed (enum) target accepts any VARCHAR width.
        self.assertTrue(gate_mod._target_compatible("VARCHAR(4)", "VARCHAR(4)"))
        self.assertFalse(gate_mod._target_compatible("VARCHAR(320)", "VARCHAR(4)"))
        self.assertFalse(gate_mod._target_compatible("VARCHAR", "VARCHAR(4)"))
        self.assertTrue(
            gate_mod._target_compatible("VARCHAR(64)", "VARCHAR + CHECK or reference table"))

    def test_byte_identical_regeneration(self):
        # Same ontology → identical Snowflake bytes, across independent generations.
        d1 = gate_mod._regen_digest(mp.map_ontology(self.onto))
        d2 = gate_mod._regen_digest(mp.map_ontology(ont.lift(json.loads(PROFILE.read_text()))))
        self.assertEqual(d1, d2)

    def test_canonical_generated_once_transpiled(self):
        ec = self.code["app_user"]
        self.assertIn("PARSE_JSON", ec.silver_sql["snowflake"])  # variant via function
        self.assertIn("JSON(", ec.silver_sql["duckdb"])          # SQLGlot → DuckDB


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestSemanticGuard(unittest.TestCase):
    def setUp(self):
        _, self.manifest, self.onto, self.maps = _load()
        self.prop_index = {(e.name, p.name): p for e in self.onto.entities for p in e.properties}

    def test_no_false_alarms_on_correct_mapping(self):
        for m in self.maps:
            self.assertEqual(
                rc.semantic_guard(self.prop_index[(m.entity, m.column)], m), [],
                f"false alarm on {m.entity}.{m.column}",
            )

    def test_guard_catches_masked_traps(self):
        # NTZ→TZ and JSON→VARCHAR reconcile cleanly under UTC but are semantic loss.
        ntz = self.prop_index[("app_user", "last_login_at")]
        bad = mp.Mapping("app_user", "last_login_at", "timestamp", ont.SEM_TIMESTAMP_NTZ,
                         "TIMESTAMP_TZ", mp.RC_TS_INSTANT, 0.9, [], [], "")
        self.assertTrue(rc.semantic_guard(ntz, bad))


@unittest.skipUnless(PROFILE.exists(), "artifacts/profile.json not generated")
class TestFrozenOntologyDeterminism(unittest.TestCase):
    """SAU-312 / CTO item ②: regeneration reads the FROZEN approved ontology and must
    NEVER re-enter the inference seam — the guarantee that holds once an LLM plugs in."""

    def setUp(self):
        _, _, self.onto, self.maps = _load()
        self.frozen = ont.freeze_ontology(self.onto)

    def test_freeze_roundtrip_is_lossless(self):
        restored = ont.load_frozen(self.frozen)
        self.assertEqual(restored.to_json(), self.onto.to_json())

    def test_frozen_envelope_is_content_addressed(self):
        self.assertEqual(self.frozen["ontology_sha256"], ont.ontology_digest(self.onto))
        self.assertEqual(self.frozen["frozen_envelope_version"], ont.FROZEN_ENVELOPE_VERSION)

    def test_regen_from_frozen_is_byte_identical_to_live(self):
        self.assertEqual(
            gate_mod._regen_digest_from_frozen(self.frozen),
            gate_mod._regen_digest(self.maps),
        )

    def test_regen_from_frozen_never_invokes_inference(self):
        # Hard proof: detonate the inference seam, then regenerate from the frozen
        # artifact. If regen re-inferred, this raises; it must not.
        before = ont.inference_call_count()
        orig = ont.infer_property

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("inference seam re-entered during frozen regen")

        ont.infer_property = _boom
        try:
            digest = gate_mod._regen_digest_from_frozen(self.frozen)
        finally:
            ont.infer_property = orig
        self.assertEqual(digest, gate_mod._regen_digest(self.maps))
        self.assertEqual(ont.inference_call_count(), before)  # zero inference calls

    def test_assert_no_inference_guard_catches_a_relift(self):
        with self.assertRaises(ont.FrozenOntologyViolation):
            with ont.assert_no_inference():
                ont.lift(json.loads(PROFILE.read_text()))

    def test_tampered_frozen_artifact_is_rejected(self):
        bad = json.loads(json.dumps(self.frozen))  # deep copy
        bad["ontology"]["entities"][0]["properties"][0]["semantic_type"] = "__tampered__"
        with self.assertRaises(ont.FrozenOntologyViolation):
            ont.load_frozen(bad)


@unittest.skipUnless(_pg_reachable(), "no Postgres reachable (set libpq env vars)")
class TestVerificationLoopDB(unittest.TestCase):
    def test_transform_bullets_pass(self):
        """Bullets 1–4 — the *transform* half of the gate — still pass on this lane.

        This assertion is unchanged in substance; it is only narrowed to say what it has
        always actually guaranteed. Bullet 0 (SAU-513) audits the *corpus* rather than the
        transform, and it currently reports drift on this lane — see the test below, which
        holds that finding explicitly instead of letting it turn this one an opaque red.
        """
        gate, _ = gate_mod.run_gate(PROFILE, MANIFEST, clean_runs=3, regen_iters=3)
        for name, b in gate.bullets.items():
            if name == "0_corpus_validity":
                continue
            self.assertTrue(b["pass"], f"§8 bullet failed: {name}: {b}")

    def test_gate_is_no_go_while_the_corpus_drift_is_open(self):
        """The Postgres lane is NO-GO, and bullet 0 is the reason (SAU-513).

        Deliberately asserted rather than skipped. `audit_log.ip_address` is seeded as
        `'203.0.113.42'` and the engine stores `203.0.113.42/32`, so a ground-truth column
        does not hold the literal its INSERT claims. Whether that is acceptable is the
        COO's call — SAU-513 puts corpus fixes explicitly out of scope. Until that call is
        made, the gate says NO-GO and this test says so out loud, so the state cannot be
        mistaken for green or quietly re-seeded away.
        """
        gate, _ = gate_mod.run_gate(PROFILE, MANIFEST, clean_runs=1, regen_iters=1)
        b0 = gate.bullets["0_corpus_validity"]
        self.assertFalse(b0["pass"])
        self.assertFalse(gate.passed)
        self.assertEqual([d["column"] for d in b0["drifted"]], ["audit_log.ip_address"])

    def test_ablation_traps_all_caught(self):
        gate, _ = gate_mod.run_gate(PROFILE, MANIFEST, clean_runs=1, regen_iters=1)
        b4 = gate.bullets["4_hard_fail_detection"]
        self.assertEqual(b4["traps_caught"], b4["traps_injected"])
        self.assertGreater(b4["traps_injected"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
