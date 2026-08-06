"""Dagster spike — medallion Bronze/Silver/Gold as a software-defined-asset graph.

What this is meant to show for the bake-off:

  * Asset-centric model is a *native* fit for medallion layers. Bronze/Silver/Gold
    are `@asset`s wired by dependency; Dagster derives the lineage DAG for free
    and renders it (data-asset lineage is the thing Dagster is built around).
  * An `@asset_check` surfaces the low-confidence mappings as a first-class,
    queryable signal on the Silver asset.
  * The human-approval gate is the *awkward* part: Dagster has no durable
    "pause this run and wait for a human for three days" primitive. The idiomatic
    move is to SPLIT the graph at the gate — materialize Bronze+Silver, let a
    human review out-of-band, then materialize Gold with the decisions supplied
    as run config. `run_dagster.py` performs exactly that two-step split, which
    is the operational cost this spike is here to expose.

Run:  python orchestration/dagster_spike/run_dagster.py
UI:   dagster dev -f orchestration/dagster_spike/definitions.py
"""

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List

import dagster as dg

# Make the shared domain module importable when run via `dagster dev -f ...`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import medallion as md  # noqa: E402


@dg.asset(group_name="bronze", description="Land raw Postgres column metadata.")
def bronze_columns(context) -> List[dict]:
    cols = md.bronze_ingest()
    context.add_output_metadata({"row_count": len(cols)})
    return [asdict(c) for c in cols]


@dg.asset(group_name="silver", description="AI lift+map -> candidate Snowflake mappings.")
def silver_mappings(context, bronze_columns: List[dict]) -> List[dict]:
    cols = [md.Column(**c) for c in bronze_columns]
    mappings = md.silver_map(cols)
    pending = md.needs_human(mappings)
    context.add_output_metadata({
        "mappings": len(mappings),
        "pending_human_review": len(pending),
        "pending_columns": dg.MetadataValue.json([m.column for m in pending]),
    })
    return [asdict(m) for m in mappings]


@dg.asset_check(asset=silver_mappings, description="Flag mappings below the confidence floor.")
def confidence_gate_check(silver_mappings: List[dict]) -> dg.AssetCheckResult:
    pending = [m for m in silver_mappings if m["approved"] is None]
    return dg.AssetCheckResult(
        passed=(len(pending) == 0),
        severity=dg.AssetCheckSeverity.WARN,
        metadata={
            "below_floor": len(pending),
            "floor": md.CONFIDENCE_FLOOR,
            "columns": dg.MetadataValue.json([m["column"] for m in pending]),
        },
    )


class GoldConfig(dg.Config):
    """Human decisions, supplied out-of-band on the *second* materialization.

    Maps "table.col" -> approve (True/False). Columns omitted here that are still
    pending are conservatively treated as rejected (fail-closed)."""
    approvals: Dict[str, bool] = {}


@dg.asset(group_name="gold", description="Codegen + reconcile approved mappings.")
def gold_sql(context,
             config: GoldConfig,
             silver_mappings: List[dict]) -> List[dict]:
    mappings = [md.Mapping(**m) for m in silver_mappings]
    for m in mappings:
        if m.approved is None:                       # decided by the human gate
            decision = config.approvals.get(m.column, False)
            m.approved = decision
            m.decided_by = "human"
    gold = md.gold_codegen_reconcile(mappings)
    context.add_output_metadata(md.summarize(mappings, gold))
    return [asdict(g) for g in gold]


defs = dg.Definitions(
    assets=[bronze_columns, silver_mappings, gold_sql],
    asset_checks=[confidence_gate_check],
)
