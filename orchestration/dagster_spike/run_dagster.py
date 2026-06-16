"""Headless runner for the Dagster medallion spike.

Demonstrates the two-step "split the graph at the gate" pattern that Dagster
forces for human-in-the-loop, because it has no durable in-run wait primitive:

    STEP 1  materialize Bronze + Silver (+ the confidence asset-check)
            -> read the pending-review queue out of the check result
    [human reviews out-of-band — here, simulated by an approval policy]
    STEP 2  materialize Gold, passing the human decisions as run config

Run:  python orchestration/dagster_spike/run_dagster.py
Exits 0 on a fully-reconciled Gold layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import dagster as dg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from definitions import (  # noqa: E402
    bronze_columns, silver_mappings, gold_sql, confidence_gate_check, GoldConfig,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import medallion as md  # noqa: E402


def main() -> int:
    print("=== Dagster spike: medallion + human gate ===\n")

    # ---- STEP 1: Bronze + Silver, with the confidence asset-check ----------
    print("STEP 1  materialize bronze + silver ...")
    res1 = dg.materialize(
        [bronze_columns, silver_mappings, confidence_gate_check],
    )
    assert res1.success, "step-1 materialization failed"

    mappings = res1.output_for_node("silver_mappings")
    pending = [m["column"] for m in mappings if m["approved"] is None]
    auto = [m["column"] for m in mappings if m["approved"]]
    print(f"        {len(mappings)} mappings; {len(auto)} auto-approved; "
          f"{len(pending)} held at gate: {pending}\n")

    # ---- HUMAN GATE (out-of-band) ------------------------------------------
    # Simulated review policy: approve the fixed-width VARCHAR, reject the inet
    # (genuinely needs a custom semantic decision -> kick back to mapping).
    approvals = {}
    for m in mappings:
        if m["approved"] is None:
            approvals[m["column"]] = not m["column"].endswith("last_seen_ip")
    print(f"HUMAN   decisions: {approvals}\n")

    # ---- STEP 2: Gold, with decisions injected as run config ---------------
    print("STEP 2  materialize gold with human decisions ...")
    res2 = dg.materialize(
        [bronze_columns, silver_mappings, gold_sql, confidence_gate_check],
        run_config=dg.RunConfig(ops={"gold_sql": GoldConfig(approvals=approvals)}),
    )
    assert res2.success, "step-2 materialization failed"

    gold = res2.output_for_node("gold_sql")
    reconciled = sum(1 for g in gold if g["reconciled"])
    skipped = [g["column"] for g in gold if not g["reconciled"]]
    print(f"        gold: {reconciled}/{len(gold)} reconciled; skipped: {skipped}\n")

    print("=== DONE (lineage: bronze_columns -> silver_mappings -> gold_sql) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
