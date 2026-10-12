# SAU-305 §8 Exit-Gate Readout — ❌ NO-GO

Phase 0/1 core loop: **lift → map → codegen → verification**. Postgres → Snowflake, reconciled on a sample via a DuckDB execution proxy (Snowflake SQL is compile-checked in the Snowflake dialect).

| # | Acceptance criterion | Result |
|---|----------------------|--------|
| 0 | Corpus validity: the seeded ground truth actually landed in the source | ❌ FAIL — 11 verified / 4 drifted (of 15 ground-truth columns) |
| 1 | 100% row-count + checksum on Gold, 3/3 clean runs | ✅ PASS — 3/3 consecutive clean; Snowflake compile errors: 0 |
| 2 | Byte-identical SQL regeneration (SQLGlot) | ✅ PASS — 5 regenerations, 1 distinct digest(s) |
| 2b | Byte-identical regen from FROZEN ontology (no re-inference) | ✅ PASS — 5 regens, 1 digest(s), matches live: True, inference re-entered: False |
| 3 | Calibrated confidence vs hard-column manifest | ❌ FAIL — 10/15 targets correct, 9/15 meet confidence floor |
| 4 | Hard-fail: surface silent loss / coercion / masked mismatch | ✅ PASS — 7/7 injected traps caught, 0 false alarms |
| 5 | Written readout for CTO go/no-go | ✅ this document |

## 0 · Corpus validity (does the ground truth exist?)

Bullets 1–4 below all validate the **transform**. None of them validates the **extraction**: reconciliation compares source-as-read to target-as-read, and both anchors sit *downstream* of anything the source engine normalized at INSERT. A trap the engine quietly rewrote is therefore invisible to them — the corpus still looks populated and the gate still goes GREEN while testing nothing. Bullet 0 anchors on the seed SQL instead, which the engine never touched.

- each ground-truth column read back from the source through the chassis' own extraction path and compared, as a multiset, against the literal its INSERT claims to plant (parsed from the seed SQL, the one anchor the source engine never rewrote)
- Seed audited: `artifacts/corpus/phase1_traps.sql` — **11 verified / 4 drifted (of 15 ground-truth columns)**.

❌ **The source does not hold what the seed claims** for the columns below. Anything downstream that relies on these columns is measuring a trap that was never planted:

| column | comparison | claimed by the seed | actually stored |
|--------|------------|---------------------|-----------------|
| attachment.upload_id | multiset | `FFFFFFFF-FFFF-4FFF-8FFF-FFFFFFFFFFFF` | `ffffffff-ffff-4fff-8fff-ffffffffffff` |
| attachment.content | multiset | `\x0001fe ff` | `\x0001feff` |
| sla_policy.response_within | multiset | `4 hours`, `30 minutes`, `1 year 2 months 3 days` | `04:00:00`, `00:30:00`, `1 year 2 mons 3 days` |
| ledger_entry.amount | multiset | `1234.56`, `0.01`, `-99999.99`, `1000000.00` | `$1,234.56`, `$0.01`, `-$99,999.99`, `$1,000,000.00` |

## 1 · Reconciliation runs

- Run 1: 27 entities, 176 columns — clean
- Run 2: 27 entities, 176 columns — clean
- Run 3: 27 entities, 176 columns — clean

## 2 · Determinism

- canonical SQL generated once per ontology; SQLGlot deterministic transpile (no LLM redraft)
- SHA-256 over all Snowflake DDL/DML across 5 regenerations: `0519188eb84a0ffd90ffdc459e123b5b162a851de5b200d70ff7e0853e9515b2`

### 2b · Frozen-ontology regeneration (SAU-312 / CTO item ②)

- Bullet 2 above re-lifts from the profile, which is byte-identical only because inference is a deterministic heuristic today. The real guarantee is this: the **approved ontology is frozen to a content-addressed artifact**, and regeneration loads that artifact and re-maps + re-codegens — the inference seam is never re-entered. The guarantee therefore survives an LLM entering inference.
- Frozen-artifact SHA-256: `6df3f6632a7c3b2cb5841ae168b13ae185786b0bfb5192e5bfaf812932580d96`
- 5 regenerations from the frozen artifact → 1 distinct digest(s); matches the live generation: True.
- Inference re-entered on the regen path: **False** (enforced by `assert_no_inference`).
- Tampered-frozen-artifact rejected by integrity check: True.

## 3 · Confidence calibration (hard columns)

| column | category | expected | got | ✓ | conf | floor | ≥floor |
|--------|----------|----------|-----|---|------|-------|--------|
| attachment.upload_id | uuid_identity | VARCHAR(36) | VARCHAR | ✗ | 0.5 | 0.9 | ✗ |
| attachment.content | binary | BINARY | VARCHAR | ✗ | 0.5 | 0.85 | ✗ |
| sla_policy.response_within | no_native_target | VARCHAR | VARCHAR | ✓ | 0.5 | 0.8 | ✗ |
| sla_policy.business_start | time_of_day | TIME | VARCHAR | ✗ | 0.5 | 0.9 | ✗ |
| ledger_entry.amount | currency_precision | NUMBER(19,2) | VARCHAR | ✗ | 0.5 | 0.9 | ✗ |
| legacy_document.body | no_native_target | VARCHAR(16777216) | VARCHAR | ✗ | 0.5 | 0.8 | ✗ |
| device_reading.sensor_no | integer | NUMBER(38,0) | NUMBER(38,0) | ✓ | 0.95 | 0.9 | ✓ |
| attachment.sha256 | fixed_width_identifier | VARCHAR(64) | VARCHAR(64) | ✓ | 0.95 | 0.95 | ✓ |
| ledger_entry.amount_exact | currency_precision | NUMBER(14,4) | NUMBER(14,4) | ✓ | 0.98 | 0.95 | ✓ |
| ledger_entry.posted_at | timestamp_tz_distinction | TIMESTAMP_NTZ | TIMESTAMP_NTZ | ✓ | 0.9 | 0.9 | ✓ |
| ledger_entry.memo_last4 | pii_fixed_width | VARCHAR(4) | VARCHAR(4) | ✓ | 0.95 | 0.95 | ✓ |
| legacy_document.doc_ref | ambiguous_text_numeric | VARCHAR(32) | VARCHAR(32) | ✓ | 0.75 | 0.7 | ✓ |
| device_reading.reading | lossy_float | FLOAT | FLOAT | ✓ | 0.7 | 0.6 | ✓ |
| device_reading.tags | array | ARRAY | ARRAY | ✓ | 0.85 | 0.8 | ✓ |
| tax_rate_history.rate | rate_precision | NUMBER(9,6) | NUMBER(9,6) | ✓ | 0.96 | 0.9 | ✓ |

## 4 · Hard-fail / trap detection (ablation)

A naive mapper was forced into each documented trap; the loop must catch every one via checksum/exec-error **or** the structural semantic guard (which defeats reconciliation-masking).

| column | naive target | checksum/exec | semantic guard | caught |
|--------|--------------|---------------|----------------|--------|
| attachment.sha256 | NUMBER(38,0) | ✓ | ✓ | ✅ |
| ledger_entry.amount_exact | FLOAT | ✓ | ✓ | ✅ |
| ledger_entry.posted_at | TIMESTAMP_TZ | · | ✓ | ✅ |
| ledger_entry.memo_last4 | NUMBER(38,0) | ✓ | ✓ | ✅ |
| legacy_document.doc_ref | NUMBER(38,0) | ✓ | ✓ | ✅ |
| device_reading.tags | VARCHAR(16777216) | · | ✓ | ✅ |
| tax_rate_history.rate | FLOAT | ✓ | ✓ | ✅ |

No semantic-guard false alarms on the correct mapping (correct run is clean — nothing is hidden).

## Caveats — what Phase 1 must still prove

This is a Phase-0 de-risk spike. The result is **necessary but not sufficient** for production; surfaced honestly so the go/no-go is not over-read:
- **No live Snowflake.** The target dialect is *compile-checked* with SQLGlot's Snowflake parser, and the medallion is *executed* on DuckDB as a stand-in. Phase 1 must re-run the identical generated Snowflake SQL on a real Snowflake account (warehouse sizing, micro-partition behavior, COPY-INTO load semantics).
- **VARIANT/ARRAY** are stored as canonical JSON text in the DuckDB proxy; their *content* preservation is reconciled and their VARIANT/ARRAY typing is compile-validated, but native semi-structured behavior is a Phase-1 confirmation.
- **Sample, not full volume.** Reconciliation ran on the seeded sample (≥15 tables, all documented traps). Phase 1 must reconcile at production volume and add scale/perf gates.
- **Inference pass is deterministic heuristic** here (reproducible, calibratable). The seam is LLM-pluggable; if an LLM is introduced it stays isolated to semantic-type inference only — never SQL generation (the reliability lever).

## Provenance — what produced this readout

A gate result is a property of the environment as much as of the code (the SQL Server lane is RED on Python 3.9 and GREEN on 3.11 with no code change). Reproduce against *this*, not against "the repo":

| key | value |
|-----|-------|
| `interpreter` | Python 3.11.12 (/usr/local/opt/python@3.11/bin/python3.11) |
| `platform` | macOS-15.7.9-x86_64-i386-64bit |
| `sqlglot` | 30.14.0 |
| `duckdb` | 1.5.5 |
| `commit` | 90ce9a5 |
| `branch` | main |
| `execution_proxy` | duckdb (duckdb 1.5.5) |
| `source.source` | PostgreSQL via psql (libpq env) |
| `source.PGHOST` | /tmp |
| `source.PGPORT` | 5599 |
| `source.PGDATABASE` | phase0 |
| `source.server_version` | PostgreSQL 14.17 (Homebrew) on x86_64-apple-darwin23.6.0, compiled by Apple clang version 16.0.0 (clang-1600.0.26.6), 64-bit |

## Verdict

**❌ NO-GO** for the §8 gate. One or more criteria failed — see above; NOT ready for Phase-1 go.
