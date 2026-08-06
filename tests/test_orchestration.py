"""SAU-304 acceptance: the human-gate semantics both spikes orchestrate.

Fast and dependency-free — exercises the shared `common.medallion` domain that
the Dagster and Temporal spikes both wrap, so the gate behaviour is pinned
regardless of which orchestrator runs it. The full runnable spikes live in
orchestration/{dagster_spike,temporal_spike}/ and are run via their own
runners (they need dagster / temporalio installed; see orchestration/README.md).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestration"))
from common import medallion as md  # noqa: E402


class TestMedallionGate(unittest.TestCase):
    def setUp(self):
        self.cols = md.bronze_ingest()
        self.mappings = md.silver_map(self.cols)

    def test_bronze_lands_sample(self):
        self.assertGreaterEqual(len(self.cols), 6)

    def test_high_confidence_auto_approved(self):
        auto = [m for m in self.mappings if m.decided_by == "auto" and m.approved]
        self.assertTrue(auto)
        for m in auto:
            self.assertGreaterEqual(m.confidence, md.CONFIDENCE_FLOOR)

    def test_low_confidence_held_at_gate(self):
        pending = {m.column for m in md.needs_human(self.mappings)}
        # The two genuinely-ambiguous types must require a human.
        self.assertIn("servers.last_seen_ip", pending)   # inet: no native SF type
        self.assertIn("accounts.tax_id", pending)        # fixed-width PII
        for m in md.needs_human(self.mappings):
            self.assertLess(m.confidence, md.CONFIDENCE_FLOOR)

    def test_gate_fails_closed_on_no_decision(self):
        # Undecided mappings must NOT reconcile (fail-closed).
        gold = md.gold_codegen_reconcile(self.mappings)
        for g in gold:
            if g.column in {"servers.last_seen_ip", "accounts.tax_id"}:
                self.assertFalse(g.reconciled)
                self.assertEqual(g.skipped_reason, "rejected at human gate")

    def test_human_decisions_flow_to_gold(self):
        for m in self.mappings:
            if m.approved is None:
                # approve everything except the inet
                m.approved = not m.column.endswith("last_seen_ip")
                m.decided_by = "human"
        gold = md.gold_codegen_reconcile(self.mappings)
        summary = md.summarize(self.mappings, gold)
        self.assertEqual(summary["rejected"], 1)            # the inet
        self.assertEqual(summary["gold_skipped"], 1)
        self.assertEqual(summary["human_decided"], 2)
        recon = {g.column for g in gold if g.reconciled}
        self.assertIn("accounts.tax_id", recon)
        self.assertNotIn("servers.last_seen_ip", recon)


if __name__ == "__main__":
    unittest.main()
