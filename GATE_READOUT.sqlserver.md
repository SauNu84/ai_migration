# SAU-325 §8 Exit-Gate Readout — ❌ NO-GO

Phase 0/1 core loop: **lift → map → codegen → verification**. SQL Server → Snowflake, reconciled on a sample via a DuckDB execution proxy (Snowflake SQL is compile-checked in the Snowflake dialect).

| # | Acceptance criterion | Result |
|---|----------------------|--------|
| 0 | Corpus validity: the seeded ground truth actually landed in the source | ❌ FAIL — 19/20 ground-truth columns intact |
| 1 | 100% row-count + checksum on Gold, 3/3 clean runs | ✅ PASS — 3/3 consecutive clean; Snowflake compile errors: 0 |
| 2 | Byte-identical SQL regeneration (SQLGlot) | ✅ PASS — 5 regenerations, 1 distinct digest(s) |
| 2b | Byte-identical regen from FROZEN ontology (no re-inference) | ✅ PASS — 5 regens, 1 digest(s), matches live: True, inference re-entered: False |
| 3 | Calibrated confidence vs hard-column manifest | ✅ PASS — 20/20 targets correct, 20/20 meet confidence floor |
| 4 | Hard-fail: surface silent loss / coercion / masked mismatch | ✅ PASS — 14/14 injected traps caught, 0 false alarms |
| 5 | Written readout for CTO go/no-go | ✅ this document |

## 0 · Corpus validity (does the ground truth exist?)

Bullets 1–4 below all validate the **transform**. None of them validates the **extraction**: reconciliation compares source-as-read to target-as-read, and both anchors sit *downstream* of anything the source engine normalized at INSERT. A trap the engine quietly rewrote is therefore invisible to them — the corpus still looks populated and the gate still goes GREEN while testing nothing. Bullet 0 anchors on the seed SQL instead, which the engine never touched.

- each ground-truth column read back from the source through the chassis' own extraction path and compared, as a multiset, against the literal its INSERT claims to plant (parsed from the seed SQL, the one anchor the source engine never rewrote)
- Seed audited: `sqlserver/schema/02_seed.sql` — **19/20** ground-truth columns hold exactly what their INSERT claims.

❌ **The source does not hold what the seed claims** for the columns below. Anything downstream that relies on these columns is measuring a trap that was never planted:

| column | comparison | claimed by the seed | actually stored |
|--------|------------|---------------------|-----------------|
| audit_trail.row_version — no INSERT names this column, so the seed claims nothing to verify | multiset | — | — |

## 1 · Reconciliation runs

- Run 1: 17 entities, 129 columns — clean
- Run 2: 17 entities, 129 columns — clean
- Run 3: 17 entities, 129 columns — clean

## 2 · Determinism

- canonical SQL generated once per ontology; SQLGlot deterministic transpile (no LLM redraft)
- SHA-256 over all Snowflake DDL/DML across 5 regenerations: `7b2b6f22fe28f017ad20230c083200ad8501013f3017c3a43b04dd1b2bfc1ac7`

### 2b · Frozen-ontology regeneration (SAU-312 / CTO item ②)

- Bullet 2 above re-lifts from the profile, which is byte-identical only because inference is a deterministic heuristic today. The real guarantee is this: the **approved ontology is frozen to a content-addressed artifact**, and regeneration loads that artifact and re-maps + re-codegens — the inference seam is never re-entered. The guarantee therefore survives an LLM entering inference.
- Frozen-artifact SHA-256: `3056f7f7d2ce663460938e3370a92a6ad136f52225d1a228c93aa84bd778e308`
- 5 regenerations from the frozen artifact → 1 distinct digest(s); matches the live generation: True.
- Inference re-entered on the regen path: **False** (enforced by `assert_no_inference`).
- Tampered-frozen-artifact rejected by integrity check: True.

## 3 · Confidence calibration (hard columns)

| column | category | expected | got | ✓ | conf | floor | ≥floor |
|--------|----------|----------|-----|---|------|-------|--------|
| policyholder.ssn_last4 | pii_fixed_width | VARCHAR(4) | VARCHAR(4) | ✓ | 0.95 | 0.95 | ✓ |
| payment_method.card_last4 | pii_fixed_width | VARCHAR(4) | VARCHAR(4) | ✓ | 0.95 | 0.95 | ✓ |
| document.file_hash | fixed_width_identifier | VARCHAR(64) | VARCHAR(64) | ✓ | 0.95 | 0.95 | ✓ |
| policy.premium | money_precision | NUMBER(19,4) | NUMBER(19,4) | ✓ | 0.98 | 0.98 | ✓ |
| claim_payment.fee | money_precision | NUMBER(10,4) | NUMBER(10,4) | ✓ | 0.98 | 0.95 | ✓ |
| journal_line.debit | money_precision | NUMBER(19,4) | NUMBER(19,4) | ✓ | 0.98 | 0.95 | ✓ |
| claim.reserve_amount | currency_as_float | FLOAT | FLOAT | ✓ | 0.75 | 0.7 | ✓ |
| journal_entry.entered_at | datetime_legacy_precision | TIMESTAMP_NTZ(3) | TIMESTAMP_NTZ(3) | ✓ | 0.9 | 0.85 | ✓ |
| journal_entry.posted_at | datetime2_precision | TIMESTAMP_NTZ(7) | TIMESTAMP_NTZ(7) | ✓ | 0.9 | 0.9 | ✓ |
| policy.effective_at | timestamp_tz | TIMESTAMP_TZ | TIMESTAMP_TZ(7) | ✓ | 0.9 | 0.9 | ✓ |
| company.created_at | timestamp_tz | TIMESTAMP_TZ | TIMESTAMP_TZ(7) | ✓ | 0.9 | 0.9 | ✓ |
| company.id | uniqueidentifier | VARCHAR(36) | VARCHAR(36) | ✓ | 0.9 | 0.9 | ✓ |
| transaction_ledger.exchange_rate | rate_precision | NUMBER(18,8) | NUMBER(18,8) | ✓ | 0.96 | 0.95 | ✓ |
| coverage.limit_amount | currency_precision | NUMBER(18,2) | NUMBER(18,2) | ✓ | 0.98 | 0.95 | ✓ |
| audit_trail.row_version | rowversion | BINARY(8) | BINARY(8) | ✓ | 0.9 | 0.9 | ✓ |
| audit_trail.ip_address | no_native_target | VARCHAR(45) | VARCHAR(45) | ✓ | 0.9 | 0.85 | ✓ |
| product_catalog.attributes_json | semi_structured | VARIANT | VARIANT | ✓ | 0.85 | 0.85 | ✓ |
| claim.metadata_json | semi_structured | VARIANT | VARIANT | ✓ | 0.85 | 0.85 | ✓ |
| policyholder.full_name | unicode_text | VARCHAR(200) | VARCHAR(200) | ✓ | 0.9 | 0.9 | ✓ |
| policy.legacy_premium_text | ambiguous_text_numeric | VARCHAR(32) | VARCHAR(32) | ✓ | 0.8 | 0.8 | ✓ |

## 4 · Hard-fail / trap detection (ablation)

A naive mapper was forced into each documented trap; the loop must catch every one via checksum/exec-error **or** the structural semantic guard (which defeats reconciliation-masking).

| column | naive target | checksum/exec | semantic guard | caught |
|--------|--------------|---------------|----------------|--------|
| policyholder.ssn_last4 | NUMBER(38,0) | ✓ | ✓ | ✅ |
| payment_method.card_last4 | NUMBER(38,0) | ✓ | ✓ | ✅ |
| document.file_hash | NUMBER(38,0) | ✓ | ✓ | ✅ |
| policy.premium | FLOAT | ✓ | ✓ | ✅ |
| claim_payment.fee | FLOAT | ✓ | ✓ | ✅ |
| journal_line.debit | FLOAT | ✓ | ✓ | ✅ |
| journal_entry.entered_at | TIMESTAMP_TZ | · | ✓ | ✅ |
| journal_entry.posted_at | TIMESTAMP_TZ | · | ✓ | ✅ |
| transaction_ledger.exchange_rate | FLOAT | ✓ | ✓ | ✅ |
| coverage.limit_amount | FLOAT | ✓ | ✓ | ✅ |
| audit_trail.row_version | TIMESTAMP_NTZ | ✓ | ✓ | ✅ |
| product_catalog.attributes_json | VARCHAR(16777216) | · | ✓ | ✅ |
| claim.metadata_json | VARCHAR(16777216) | · | ✓ | ✅ |
| policy.legacy_premium_text | NUMBER(38,0) | ✓ | ✓ | ✅ |

No semantic-guard false alarms on the correct mapping (correct run is clean — nothing is hidden).

## Caveats — what Phase 1 must still prove

This is a Phase-0 de-risk spike. The result is **necessary but not sufficient** for production; surfaced honestly so the go/no-go is not over-read:
- **No live Snowflake.** The target dialect is *compile-checked* with SQLGlot's Snowflake parser, and the medallion is *executed* on DuckDB as a stand-in. Phase 1 must re-run the identical generated Snowflake SQL on a real Snowflake account (warehouse sizing, micro-partition behavior, COPY-INTO load semantics).
- **VARIANT/ARRAY** are stored as canonical JSON text in the DuckDB proxy; their *content* preservation is reconciled and their VARIANT/ARRAY typing is compile-validated, but native semi-structured behavior is a Phase-1 confirmation.
- **Sample, not full volume.** Reconciliation ran on the seeded sample (≥15 tables, all documented traps). Phase 1 must reconcile at production volume and add scale/perf gates.
- **Inference pass is deterministic heuristic** here (reproducible, calibratable). The seam is LLM-pluggable; if an LLM is introduced it stays isolated to semantic-type inference only — never SQL generation (the reliability lever).
- **MONEY/SMALLMONEY 4-dp** is preserved end-to-end: the extraction wire form casts through `DECIMAL(38,4)` (defeating the default 2-dp `CONVERT`), Bronze lands the full scale-4 text, and Silver maps to `NUMBER(p,4)` — reconciled byte-exact (the 4th-dp and the high-precision boundary 999999.9999 both round-trip clean). This trap is fully proven on the proxy.
- **DATETIME2(7) 100-ns** is preserved by the extraction wire form (ISO-8601 style 126 emits all 7 fractional digits) and asserted by the `TIMESTAMP_NTZ(7)` target type (Snowflake compile-checked). BUT the DuckDB execution proxy and Python `datetime` are microsecond-precision, so the reconciliation verifies the value at µs on both sides — the 7th digit's *live* round-trip is a Phase-1 confirmation on real Snowflake, not something the proxy can show. The 100-ns is not silently lost (the wire form keeps it); it is simply not yet re-executed at full scale.
- **Rig adapter.** The lane ran against the containerized SQL Server 2022 seed (SAU-323) via `sqlserver/scripts/sqlcmd-rig.sh` (the image's bundled sqlcmd over `docker exec`, since no host sqlcmd is installed). The dialect's wire flags were also corrected to `-y 0` — the classic `-h -1 -y 0 -W` combo is rejected by that image's sqlcmd. Target rig is unchanged (target-agnostic source swap); when the live-Snowflake rig (SAU-308/309/310) lands, this lane inherits it for free.

## Verdict

**❌ NO-GO** for the §8 gate. One or more criteria failed — see above; NOT ready for Phase-1 go.
