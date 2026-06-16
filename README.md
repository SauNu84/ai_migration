# ai_migration — Phase 0 de-risk spike

Proving the **AI-mapping verification loop** for **Postgres → Snowflake** on one
realistic schema, end-to-end (board brief §8). This repo holds:

* **child #1** ([SAU-303](https://github.com/SauNu84/ai_migration)) — the *input side*:
  a realistic test schema, seeded ground-truth hard columns, and a Postgres profiling
  harness emitting a structured profile artifact the core loop consumes.
* **child #2** ([SAU-305](https://github.com/SauNu84/ai_migration)) — the *core IP and
  §8 deliverable*: the **lift → map → codegen → verification** loop and the exit-gate
  scorecard (`GATE_READOUT.md`).

## Layout

```
schema/01_schema.sql      16-table B2B-SaaS schema: real FKs + deliberate type traps
schema/02_seed.sql        deterministic seed exercising every hard column
profile/profile.sql       phase0_profile() — in-DB statistical profiling (SQL pushdown)
profile/profile.py        orchestrator → artifacts/profile.json (no data egress)
ground_truth/hard_columns.json   manifest: hard columns + expected Snowflake target (§8 calibration)
scripts/setup.sh          one-command bring-up (create db → schema → seed → profile)
core/ontology.py          LIFT: profile → typed property graph + isolated semantic inference
core/mapper.py            MAP: ontology property → Snowflake target + confidence
core/codegen.py           CODEGEN: canonical SQL once → SQLGlot transpile (Snowflake + DuckDB)
core/reconcile.py         VERIFY: execute medallion on sample, reconcile rowcount+checksum, guards
core/gate.py              §8 exit gate orchestration + written readout
run_loop.py               driver → ontology/mappings/SQL artifacts + GATE_READOUT.md
tests/test_profile.py     SAU-303 acceptance tests
tests/test_core_loop.py   SAU-305 §8 acceptance tests
artifacts/                generated (gitignored): profile.json, ontology.json, mappings.json, sql/
```

## The core loop (SAU-305)

```bash
pip install -r requirements.txt          # sqlglot + duckdb
PGHOST=/tmp PGPORT=5599 PGUSER=postgres ./scripts/setup.sh   # build source + profile
PGHOST=/tmp PGPORT=5599 PGUSER=postgres python3 run_loop.py  # lift→map→codegen→verify→gate
PGHOST=/tmp PGPORT=5599 PGUSER=postgres python3 -m unittest tests.test_core_loop -v
```

`run_loop.py` exits 0 only when all four §8 technical criteria pass and writes
`GATE_READOUT.md` (the pass/fail readout for the CTO Phase-1 go/no-go).

**Reliability lever:** canonical SQL is generated **once** per approved ontology, then
**SQLGlot** transpiles it deterministically to Snowflake (compile-checked, the
byte-identical-regeneration artifact) and DuckDB (the execution proxy reconciliation
runs against — no live Snowflake in Phase 0). The AI is isolated to the
semantic-inference pass only; it never writes SQL. The verification loop has two
detectors: **data reconciliation** (row-count + per-column checksum on Gold) and a
**structural semantic guard** that surfaces type-semantics changes (tz-awareness flip,
text→numeric coercion, scale reduction, JSON/array flattening) even when sample data
coincidentally round-trips — so a mapping can't *mask* a semantic mismatch.

## Quickstart

Requires `psql` and `python3` on PATH and a reachable Postgres. Connection uses the
standard libpq env vars.

```bash
# Example: local Postgres on port 5599
PGPORT=5599 PGUSER=postgres PGPASSWORD=postgres ./scripts/setup.sh

# Run the acceptance tests
PGPORT=5599 PGUSER=postgres PGPASSWORD=postgres python3 -m unittest tests.test_profile -v
```

`setup.sh` is reproducible — re-running drops and rebuilds the schema, reseeds, and
regenerates `artifacts/profile.json`.

## What the profile artifact contains

For every base table: `row_count`, `column_count`, and per-column `data_type`/`udt`,
nullability, char-length / numeric precision+scale, and **statistics computed in-DB**
(`null_count`, `null_rate`, `distinct_count`, `min`/`max` as text). Plus schema-wide
`foreign_keys` (relationship signals). **No raw rows and no value samples leave the
database** — only aggregate statistics — so PII never egresses.

## Hard columns (ground truth)

`ground_truth/hard_columns.json` is the calibration target for the core verification
loop. It documents 20 deliberately hard columns spanning currency precision, tz vs
non-tz timestamps, semi-structured JSON, arrays, fixed-width PII (leading zeros),
`inet` (no native Snowflake type), ambiguous numeric-looking text, enums, and lossy
floats — each with its expected Snowflake target, the trap a naive mapper falls into,
the verification assertion, and a calibrated `confidence_floor`.

## Exit-gate relevance (§8)

This is the *input* the §8 go/no-go runs against: a ≥15-table schema with real FKs and
type edge cases, plus the ground truth needed to score the mapper's confidence
calibration. The reconciliation / byte-identical-regeneration checks live in the core
loop (child #2), which consumes `artifacts/profile.json` and this manifest.
