"""Orchestrator-agnostic domain logic for the SAU-304 bake-off.

This module is the *work*. Both the Dagster spike and the Temporal spike wrap
these exact functions, so the bake-off compares orchestration mechanics on
identical units of work rather than on differently-shaped toy programs.

The shape mirrors the real Phase-0 verification loop (see repo README):

    Bronze   ingest the Postgres profile artifact  (raw landing)
    Silver   AI lift + map  ->  candidate Snowflake mappings + confidence
    [GATE]   low-confidence mappings require a human approval decision
    Gold     codegen + reconcile  ->  verified, transpiled SQL

Nothing here imports Dagster or Temporal. It has no I/O beyond reading the
optional profile artifact, and is deterministic so the spikes are reproducible.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# Confidence at or above this is auto-approved; below it needs a human.
CONFIDENCE_FLOOR = 0.85


# --------------------------------------------------------------------------- #
# Data carried between medallion layers
# --------------------------------------------------------------------------- #
@dataclass
class Column:
    table: str
    name: str
    pg_type: str


@dataclass
class Mapping:
    column: str          # "table.name"
    pg_type: str
    snowflake_type: str
    confidence: float
    rationale: str
    # Filled in by the gate; None until decided.
    approved: Optional[bool] = None
    decided_by: str = "auto"


@dataclass
class GoldResult:
    column: str
    sql: str
    reconciled: bool
    skipped_reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# A small, representative slice of the §8 "hard columns". Used when no live
# profile artifact is present, so both spikes run with zero external setup.
# --------------------------------------------------------------------------- #
_SAMPLE_COLUMNS = [
    Column("invoices", "amount_due", "numeric(12,2)"),
    Column("invoices", "issued_at", "timestamptz"),
    Column("events", "payload", "jsonb"),
    Column("accounts", "tax_id", "char(9)"),          # fixed-width, leading zeros
    Column("servers", "last_seen_ip", "inet"),         # no native Snowflake type -> hard
    Column("metrics", "sample_rate", "double precision"),
]


def _confidence_for(pg_type: str) -> tuple[str, float, str]:
    """Toy AI mapper. Deterministic. Returns (snowflake_type, confidence, why).

    The two genuinely-ambiguous types (inet, char(9)) land *below* the floor on
    purpose, so every run exercises the human-approval gate.
    """
    t = pg_type.lower()
    if t.startswith("numeric"):
        return "NUMBER(12,2)", 0.98, "exact precision/scale preserved"
    if t == "timestamptz":
        return "TIMESTAMP_TZ", 0.95, "tz-aware -> TIMESTAMP_TZ"
    if t == "jsonb":
        return "VARIANT", 0.90, "semi-structured -> VARIANT"
    if t == "double precision":
        return "FLOAT", 0.88, "lossy float preserved as FLOAT"
    if t.startswith("char"):
        # fixed-width w/ leading zeros: VARCHAR loses the width guarantee
        return "VARCHAR(9)", 0.72, "fixed-width PII; width must be asserted, not inferred"
    if t == "inet":
        # no native Snowflake type at all -> genuinely needs a human call
        return "VARCHAR(43)", 0.55, "no native INET type; VARCHAR vs custom semantic - human call"
    return "VARCHAR", 0.60, "unknown type; defaulting to VARCHAR"


# --------------------------------------------------------------------------- #
# BRONZE
# --------------------------------------------------------------------------- #
def bronze_ingest(profile_path: Optional[str] = None) -> List[Column]:
    """Land raw column metadata.

    Self-contained by default: returns the embedded representative sample so both
    spikes run with zero external setup (and identically). Reading a real profile
    artifact is opt-in via the `PROFILE_PATH` env var or an explicit argument."""
    path = profile_path or os.environ.get("PROFILE_PATH")
    if path and os.path.exists(path):
        with open(path) as fh:
            profile = json.load(fh)
        cols: List[Column] = []
        for tbl in profile.get("tables", []):
            tname = tbl.get("name", "?")
            for c in tbl.get("columns", []):
                cols.append(Column(tname, c["name"], c.get("data_type", "unknown")))
        if cols:
            return cols
    return list(_SAMPLE_COLUMNS)


# --------------------------------------------------------------------------- #
# SILVER  (AI lift + map)
# --------------------------------------------------------------------------- #
def silver_map(columns: List[Column]) -> List[Mapping]:
    mappings: List[Mapping] = []
    for col in columns:
        sf_type, conf, why = _confidence_for(col.pg_type)
        m = Mapping(
            column=f"{col.table}.{col.name}",
            pg_type=col.pg_type,
            snowflake_type=sf_type,
            confidence=conf,
            rationale=why,
        )
        if conf >= CONFIDENCE_FLOOR:
            m.approved = True
            m.decided_by = "auto"
        mappings.append(m)
    return mappings


def needs_human(mappings: List[Mapping]) -> List[Mapping]:
    """Mappings still awaiting a human decision (the gate's work queue)."""
    return [m for m in mappings if m.approved is None]


# --------------------------------------------------------------------------- #
# GOLD  (codegen + reconcile) — only runs on approved mappings
# --------------------------------------------------------------------------- #
def gold_codegen_reconcile(mappings: List[Mapping]) -> List[GoldResult]:
    results: List[GoldResult] = []
    for m in mappings:
        if not m.approved:
            results.append(
                GoldResult(m.column, sql="", reconciled=False,
                           skipped_reason="rejected at human gate")
            )
            continue
        col_name = m.column.split(".")[-1]
        sql = f'CAST("{col_name}" AS {m.snowflake_type}) AS "{col_name}"'
        # In the real loop this is a row-count + checksum reconciliation against
        # DuckDB. Here we treat a well-formed cast as reconciled.
        results.append(GoldResult(m.column, sql=sql, reconciled=True))
    return results


def summarize(mappings: List[Mapping], gold: List[GoldResult]) -> Dict:
    return {
        "columns": len(mappings),
        "auto_approved": sum(1 for m in mappings if m.decided_by == "auto" and m.approved),
        "human_decided": sum(1 for m in mappings if m.decided_by != "auto"),
        "rejected": sum(1 for m in mappings if m.approved is False),
        "gold_reconciled": sum(1 for g in gold if g.reconciled),
        "gold_skipped": sum(1 for g in gold if not g.reconciled),
    }


if __name__ == "__main__":
    cols = bronze_ingest()
    maps = silver_map(cols)
    print(f"bronze: {len(cols)} columns")
    print(f"silver: {len(maps)} mappings, {len(needs_human(maps))} need human review")
    for m in needs_human(maps):
        # default policy when run headless: approve below-floor mappings as
        # VARCHAR fallbacks so the demo completes end-to-end.
        m.approved, m.decided_by = True, "headless-default"
    gold = gold_codegen_reconcile(maps)
    print("summary:", json.dumps(summarize(maps, gold), indent=2))
