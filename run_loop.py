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
from core import dialect as dl
from core import target as tg

REPO = Path(__file__).resolve().parent

# Selectable execution targets (SAU-432). DuckDB is the default proxy (byte-for-byte
# with SAU-305); Snowflake is the live path, guarded until SAU-308 creds land (SAU-309
# flips `live=True`). Both run the same committed Snowflake SQL — selecting a target
# never edits a byte of the generated artifacts.
_TARGETS = {
    "duckdb": lambda: tg.DuckDbAdapter(),
    "snowflake": lambda: tg.SnowflakeAdapter(live=True),
}

# Per-source-lane defaults: profile artifact, hard-column manifest, readout labels.
_LANES = {
    "postgres": {
        "dialect": dl.PostgresDialect,
        "profile": REPO / "artifacts" / "profile.json",
        "manifest": REPO / "ground_truth" / "hard_columns.json",
        "seed": REPO / "schema" / "02_seed.sql",
        "readout": REPO / "GATE_READOUT.md",
        "issue": "SAU-305", "source_label": "Postgres", "caveats": [],
    },
    "sqlserver": {
        "dialect": dl.SqlServerDialect,
        "profile": REPO / "artifacts" / "profile.sqlserver.json",
        "manifest": REPO / "sqlserver" / "ground_truth" / "hard_columns.sqlserver.json",
        "seed": REPO / "sqlserver" / "schema" / "02_seed.sql",
        "readout": REPO / "GATE_READOUT.sqlserver.md",
        "issue": "SAU-325", "source_label": "SQL Server",
        "caveats": [
            "**MONEY/SMALLMONEY 4-dp** is preserved end-to-end: the extraction wire form "
            "casts through `DECIMAL(38,4)` (defeating the default 2-dp `CONVERT`), Bronze "
            "lands the full scale-4 text, and Silver maps to `NUMBER(p,4)` — reconciled "
            "byte-exact (the 4th-dp and the high-precision boundary 999999.9999 both "
            "round-trip clean). This trap is fully proven on the proxy.",
            "**DATETIME2(7) 100-ns** is preserved by the extraction wire form (ISO-8601 "
            "style 126 emits all 7 fractional digits) and asserted by the `TIMESTAMP_NTZ(7)` "
            "target type (Snowflake compile-checked). BUT the DuckDB execution proxy and "
            "Python `datetime` are microsecond-precision, so the reconciliation verifies "
            "the value at µs on both sides — the 7th digit's *live* round-trip is a "
            "Phase-1 confirmation on real Snowflake, not something the proxy can show. "
            "The 100-ns is not silently lost (the wire form keeps it); it is simply not "
            "yet re-executed at full scale.",
            "**Rig adapter.** The lane ran against the containerized SQL Server 2022 seed "
            "(SAU-323) via `sqlserver/scripts/sqlcmd-rig.sh` (the image's bundled sqlcmd "
            "over `docker exec`, since no host sqlcmd is installed). The dialect's wire "
            "flags were also corrected to `-y 0` — the classic `-h -1 -y 0 -W` combo is "
            "rejected by that image's sqlcmd. Target rig is unchanged (target-agnostic "
            "source swap); when the live-Snowflake rig (SAU-308/309/310) lands, this "
            "lane inherits it for free.",
        ],
    },
}


def main() -> int:
    ap = argparse.ArgumentParser(description="lift→map→codegen→verify §8 gate loop")
    ap.add_argument("--dialect", choices=sorted(_LANES), default="postgres",
                    help="source lane (selects profile/manifest/readout defaults)")
    ap.add_argument("--target", choices=sorted(_TARGETS), default="duckdb",
                    help="execution target (duckdb proxy [default] or live snowflake)")
    ap.add_argument("--profile")
    ap.add_argument("--manifest")
    ap.add_argument("--seed", help="seed SQL audited by gate bullet 0 (corpus validity)")
    ap.add_argument("--out", default=str(REPO / "artifacts"))
    ap.add_argument("--readout")
    ap.add_argument("--clean-runs", type=int, default=3)
    args = ap.parse_args()

    lane = _LANES[args.dialect]
    dialect = lane["dialect"]()
    target = _TARGETS[args.target]()
    profile_path = args.profile or str(lane["profile"])
    manifest_path = args.manifest or str(lane["manifest"])
    seed_path = args.seed or str(lane["seed"])
    readout_path = args.readout or str(lane["readout"])

    out = Path(args.out)
    (out / "sql").mkdir(parents=True, exist_ok=True)

    # Persist the lifted ontology + mappings + generated SQL (the approvable artifacts).
    profile = ont.load_profile(profile_path)
    onto = ont.lift(profile, dialect=dialect)
    # Freeze the approved ontology at the human-approval boundary (SAU-312), then run the
    # canonical codegen path FROM the frozen artifact — never re-inferring. The guard
    # makes that discipline executable, not just documented.
    frozen = ont.freeze_ontology(onto)
    with ont.assert_no_inference():
        maps = mp.map_ontology(ont.load_frozen(frozen))
        code = cg.generate(maps)
    suffix = "" if args.dialect == "postgres" else f".{args.dialect}"
    (out / f"ontology{suffix}.json").write_text(json.dumps(onto.to_json(), indent=2) + "\n")
    (out / f"ontology.frozen{suffix}.json").write_text(json.dumps(frozen, indent=2) + "\n")
    (out / f"mappings{suffix}.json").write_text(
        json.dumps([m.to_json() for m in maps], indent=2) + "\n"
    )
    sqldir = out / ("sql" if not suffix else f"sql{suffix}")
    sqldir.mkdir(parents=True, exist_ok=True)
    for entity, ec in code.items():
        (sqldir / f"{entity}.canonical.sql").write_text(ec.canonical_silver + "\n")
        (sqldir / f"{entity}.snowflake.sql").write_text(
            "\n\n".join([ec.bronze_ddl["snowflake"], ec.silver_sql["snowflake"],
                         ec.gold_sql["snowflake"]]) + "\n"
        )
        (sqldir / f"{entity}.duckdb.sql").write_text(
            "\n\n".join([ec.bronze_ddl["duckdb"], ec.silver_sql["duckdb"],
                         ec.gold_sql["duckdb"]]) + "\n"
        )

    # Run the §8 gate.
    gate, detail = gate_mod.run_gate(profile_path, manifest_path,
                                     clean_runs=args.clean_runs, dialect=dialect,
                                     target=target, seed_path=seed_path)
    (out / f"gate_result{suffix}.json").write_text(
        json.dumps(gate.bullets, indent=2, default=str) + "\n"
    )
    readout = gate_mod.write_readout(gate, readout_path, issue=lane["issue"],
                                     source_label=lane["source_label"],
                                     extra_caveats=lane["caveats"],
                                     provenance=gate_mod.provenance(dialect, target))
    print(readout)
    print(f"\n[run_loop] artifacts → {out}  |  readout → {readout_path}")
    return 0 if gate.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
