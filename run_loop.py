#!/usr/bin/env python3
"""SAU-305 core loop driver — lift → map → codegen → verify → §8 gate.

Consumes the SAU-303 profile artifact + the hard-column manifest, runs the full
verification loop, and writes:
  * artifacts/ontology.json   — the lifted typed property graph (human-approvable)
  * artifacts/mappings.json    — per-column Snowflake mappings + confidence
  * artifacts/sql/<entity>.*   — generated canonical / Snowflake / DuckDB SQL
  * artifacts/gate_result.json — machine-readable §8 scorecard
  * GATE_READOUT.md            — the written pass/fail readout for the CTO go/no-go

Connection to the source Postgres uses the standard libpq env vars (same as the
SAU-303 harness). Usage:
    PGHOST=/tmp PGPORT=5599 PGUSER=postgres python3 run_loop.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core import ontology as ont
from core import mapper as mp
from core import codegen as cg
from core import gate as gate_mod

REPO = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description="SAU-305 lift→map→codegen→verify loop")
    ap.add_argument("--profile", default=str(REPO / "artifacts" / "profile.json"))
    ap.add_argument("--manifest", default=str(REPO / "ground_truth" / "hard_columns.json"))
    ap.add_argument("--out", default=str(REPO / "artifacts"))
    ap.add_argument("--readout", default=str(REPO / "GATE_READOUT.md"))
    ap.add_argument("--clean-runs", type=int, default=3)
    args = ap.parse_args()

    out = Path(args.out)
    (out / "sql").mkdir(parents=True, exist_ok=True)

    # Persist the lifted ontology + mappings + generated SQL (the approvable artifacts).
    profile = ont.load_profile(args.profile)
    onto = ont.lift(profile)
    # Freeze the approved ontology at the human-approval boundary (SAU-312), then run the
    # canonical codegen path FROM the frozen artifact — never re-inferring. The guard
    # makes that discipline executable, not just documented.
    frozen = ont.freeze_ontology(onto)
    with ont.assert_no_inference():
        maps = mp.map_ontology(ont.load_frozen(frozen))
        code = cg.generate(maps)
    (out / "ontology.json").write_text(json.dumps(onto.to_json(), indent=2) + "\n")
    (out / "ontology.frozen.json").write_text(json.dumps(frozen, indent=2) + "\n")
    (out / "mappings.json").write_text(
        json.dumps([m.to_json() for m in maps], indent=2) + "\n"
    )
    for entity, ec in code.items():
        (out / "sql" / f"{entity}.canonical.sql").write_text(ec.canonical_silver + "\n")
        (out / "sql" / f"{entity}.snowflake.sql").write_text(
            "\n\n".join([ec.bronze_ddl["snowflake"], ec.silver_sql["snowflake"],
                         ec.gold_sql["snowflake"]]) + "\n"
        )
        (out / "sql" / f"{entity}.duckdb.sql").write_text(
            "\n\n".join([ec.bronze_ddl["duckdb"], ec.silver_sql["duckdb"],
                         ec.gold_sql["duckdb"]]) + "\n"
        )

    # Run the §8 gate.
    gate, detail = gate_mod.run_gate(args.profile, args.manifest, clean_runs=args.clean_runs)
    (out / "gate_result.json").write_text(
        json.dumps(gate.bullets, indent=2, default=str) + "\n"
    )
    readout = gate_mod.write_readout(gate, args.readout)
    print(readout)
    print(f"\n[run_loop] artifacts → {out}  |  readout → {args.readout}")
    return 0 if gate.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
