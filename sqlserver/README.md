# SQL Server lane — trap-rich source rig (SAU-323)

The SQL Server analogue of the Postgres input side ([SAU-303](../README.md)). It
stands up a **real, containerized SQL Server source** seeded with a deliberate
**data-corruption trap corpus** and a **20-column ground-truth manifest**, so the §8
gate can be validated on a trap-rich SQL Server source the same way it is on the
Postgres `:5599` rig. Parent: SAU-321. Consumed by SAU-321 children 3 & 4 (the SQL
Server `SourceDialect` and the cross-dialect gate run).

## Layout

```
sqlserver/
  docker-compose.yml                    containerized SQL Server 2022 (host port 14333)
  scripts/setup.sh                      one-command bring-up (up → create db → schema → seed → verify)
  schema/01_schema.sql                  17-table GL/policy/claims/transactions schema: real FKs + T-SQL type traps
  schema/02_seed.sql                    deterministic seed exercising every hard column
  ground_truth/hard_columns.sqlserver.json   20-column manifest: source type, Snowflake target, trap, assert, detection, confidence floor
  ground_truth/trap_corpus.md           human-readable expected-catch manifest (trap → column → seed value → detector)
```

## Bring it up

Requires Docker (Compose v2). No host `sqlcmd` needed — `setup.sh` uses a throwaway
`mssql-tools18` container if one isn't on PATH.

```bash
# Start the server + apply schema + seed + verify (reproducible: re-run rebuilds).
./sqlserver/scripts/setup.sh

# Tear down (drops the data volume too).
docker compose -f sqlserver/docker-compose.yml down -v
```

Defaults (override via env): host `localhost`, port `14333`, db `phase1`, SA password
`Phase1_Str0ng!Pass` (dev-only — set `MSSQL_SA_PASSWORD` for anything real). To point
the rig at an already-running server instead of Compose, use
`NO_COMPOSE=1 ./sqlserver/scripts/setup.sh`.

`setup.sh` is idempotent: `01_schema.sql` drops the tables (child→parent order) before
recreating them, and the seed is deterministic, so re-running fully rebuilds the source.

## The schema (17 tables)

A GL / policy / claims / financial-transactions domain (distinct from the Postgres
B2B-SaaS schema, so the gate is proven on a *second* shape):

`company` · `gl_account` · `fiscal_period` · `journal_entry` · `journal_line` ·
`policyholder` · `agent` · `product_catalog` · `policy` · `coverage` · `claim` ·
`claim_payment` · `payment_method` · `transaction_ledger` · `exchange_rate` ·
`document` · `audit_trail`

Real foreign keys throughout (GUID and IDENTITY parents), FK indexes for relationship
signal, and CHECK-enforced domains (`account_type`, `status`, `kind`) standing in for
the "no native enum" mapping decision.

## What each trap proves

See [`ground_truth/trap_corpus.md`](ground_truth/trap_corpus.md) for the full
expected-catch manifest. In short, the corpus exercises every SQL-Server trap class
the §8 gate must catch:

| Trap class | Proven by | Why it matters |
|---|---|---|
| `MONEY` / `SMALLMONEY` 4-dp drop | `policy.premium`, `journal_line.debit`, `claim_payment.fee` | default CONVERT/2-dp/FLOAT silently loses the 4th decimal |
| `DATETIME` vs `DATETIME2` | `journal_entry.entered_at` (3.33 ms tick) vs `posted_at` (100-ns) | precision is invented or truncated by the wrong target |
| `DATETIMEOFFSET` tz flips | `policy.effective_at`, `company.created_at` | dropping/flipping the offset shifts the instant |
| `UNIQUEIDENTIFIER` | `company.id` | GUID must stay canonical text or FK joins break |
| `NVARCHAR` width/collation | `policyholder.full_name`, `document.title_cs` | unicode + case sensitivity must survive |
| `ROWVERSION` | `audit_trail.row_version` | the name "timestamp" is a trap — it's `BINARY(8)` |
| implicit DECIMAL precision | `transaction_ledger.exchange_rate` (18,8), `coverage.limit_amount` (18,2) | high-scale rates / large currency lose precision under FLOAT |
| currency-as-FLOAT | `claim.reserve_amount` | money-in-float must be flagged lossy, low confidence |
| fixed-width PII coercion | `policyholder.ssn_last4`, `payment_method.card_last4`, `document.file_hash` | numeric coercion eats leading zeros |
| JSON flattening | `product_catalog.attributes_json`, `claim.metadata_json` | NVARCHAR(MAX) JSON → VARIANT, not flattened VARCHAR |
| no native target | `audit_trail.ip_address` | IPv4 + IPv6 text serialize losslessly |
| ambiguous text-numeric | `policy.legacy_premium_text` | `899,50` must not be auto-parsed to a number |

## Ground truth (calibration target)

`ground_truth/hard_columns.sqlserver.json` is the 20-column calibration target: for
each hard column it records the source `tsql_type`, the EXPECTED-correct
`snowflake_target`, the `trap` a naive mapper falls into, the verification `assert`,
the `detection` (which §8 detector — `checksum` and/or `semantic_guard` — should catch
a naive mapping), and a calibrated `confidence_floor`. It mirrors the field shape of
the Postgres `ground_truth/hard_columns.json` (with `pg_type` → `tsql_type`) so the
core loop can score confidence calibration identically across both lanes.

## Exit-gate relevance (§8)

This is the SQL Server *input* the §8 go/no-go runs against: a ≥15-table schema with
real FKs and SQL-Server-specific type edge cases, plus the ground truth needed to score
the mapper's confidence calibration and prove the hard-fail detectors fire on naive
mappings (and only on naive mappings). The profiler, lift→map→codegen→verify loop, and
reconciliation detectors are dialect-agnostic (`core/`), reached through the
`SourceDialect` seam (SAU-322); the SQL Server implementation of that seam is SAU-321
child 3, which consumes this rig.
