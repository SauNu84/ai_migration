# ai_migration — Phase 0 de-risk spike

Proving the **AI-mapping verification loop** for **Postgres → Snowflake** on one
realistic schema, end-to-end (board brief §8). This repo currently holds **Phase 0,
child #1** ([SAU-303](https://github.com/SauNu84/ai_migration)): the *input side* — a
realistic test schema, seeded ground-truth hard columns, and a Postgres profiling
harness that emits a structured profile artifact the core loop consumes.

## Layout

```
schema/01_schema.sql      16-table B2B-SaaS schema: real FKs + deliberate type traps
schema/02_seed.sql        deterministic seed exercising every hard column
profile/profile.sql       phase0_profile() — in-DB statistical profiling (SQL pushdown)
profile/profile.py        orchestrator → artifacts/profile.json (no data egress)
ground_truth/hard_columns.json   manifest: hard columns + expected Snowflake target (§8 calibration)
scripts/setup.sh          one-command bring-up (create db → schema → seed → profile)
tests/test_profile.py     acceptance tests (manifest + profile output vs ground truth)
artifacts/profile.json    generated profile (gitignored)
```

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
