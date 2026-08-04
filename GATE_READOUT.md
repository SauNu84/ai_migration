# SAU-305 §8 Exit-Gate Readout — ✅ GO

Phase 0/1 core loop: **lift → map → codegen → verification**. Postgres → Snowflake, reconciled on a sample via a DuckDB execution proxy (Snowflake SQL is compile-checked in the Snowflake dialect).

| # | Acceptance criterion | Result |
|---|----------------------|--------|
| 0 | Corpus validity: the seeded ground truth actually landed in the source | ✅ PASS — 20 verified / 0 drifted (of 20 ground-truth columns) |
| 1 | 100% row-count + checksum on Gold, 3/3 clean runs | ✅ PASS — 3/3 consecutive clean; Snowflake compile errors: 0 |
| 2 | Byte-identical SQL regeneration (SQLGlot) | ✅ PASS — 5 regenerations, 1 distinct digest(s) |
| 2b | Byte-identical regen from FROZEN ontology (no re-inference) | ✅ PASS — 5 regens, 1 digest(s), matches live: True, inference re-entered: False |
| 3 | Calibrated confidence vs hard-column manifest | ✅ PASS — 20/20 targets correct, 20/20 meet confidence floor |
| 4 | Hard-fail: surface silent loss / coercion / masked mismatch | ✅ PASS — 15/15 injected traps caught, 0 false alarms |
| 5 | Written readout for CTO go/no-go | ✅ this document |

## 0 · Corpus validity (does the ground truth exist?)

Bullets 1–4 below all validate the **transform**. None of them validates the **extraction**: reconciliation compares source-as-read to target-as-read, and both anchors sit *downstream* of anything the source engine normalized at INSERT. A trap the engine quietly rewrote is therefore invisible to them — the corpus still looks populated and the gate still goes GREEN while testing nothing. Bullet 0 anchors on the seed SQL instead, which the engine never touched.

- each ground-truth column read back from the source through the chassis' own extraction path and compared, as a multiset, against the literal its INSERT claims to plant (parsed from the seed SQL, the one anchor the source engine never rewrote)
- Seed audited: `schema/02_seed.sql` — **20 verified / 0 drifted (of 20 ground-truth columns)**.

No drift: every ground-truth column round-trips the literal its INSERT claims, so the traps the bullets below rely on are genuinely present.

### Reconciled under a named type equivalence

The pairs below did **not** match byte-for-byte. Each was reconciled by a rule the source dialect declares for that type — documented type semantics, derivable without ever looking at our corpus — not by editing what the seed claims. Raw forms are printed on both sides so the rule is auditable:

| column | claimed by the seed | actually stored | rule |
|--------|---------------------|-----------------|------|
| audit_log.ip_address | `203.0.113.42` | `203.0.113.42/32` | `inet_host_mask` |
| audit_log.ip_address | `198.51.100.7` | `198.51.100.7/32` | `inet_host_mask` |
| audit_log.ip_address | `::1` | `::1/128` | `inet_host_mask` |
| audit_log.ip_address | `2001:db8::8a2e:370:7334` | `2001:db8::8a2e:370:7334/128` | `inet_host_mask` |

Read the direction, not just the rows. Each rule is consulted only on residuals the exact comparison left over, and only the way the type documents — the seed abbreviates, the type expands. The inverse, an extraction that **strips** what the type stores, would make every abbreviated literal match exactly and empty this table: a strictly greener readout produced by a lossy write policy. So the corpus deliberately also seeds values no equivalence can reach (ADR-0001, SAU-518) — those rows match byte-for-byte here and fail loudly the moment extraction starts discarding.

## 1 · Reconciliation runs

- Run 1: 16 entities, 123 columns — clean
- Run 2: 16 entities, 123 columns — clean
- Run 3: 16 entities, 123 columns — clean

## 2 · Determinism

- canonical SQL generated once per ontology; SQLGlot deterministic transpile (no LLM redraft)
- SHA-256 over all Snowflake DDL/DML across 5 regenerations: `fb6dc1f0ef7fdf1688c4fe13b21b2e977612931d535b4615b6f41918f12e5dd6`

### 2b · Frozen-ontology regeneration (SAU-312 / CTO item ②)

- Bullet 2 above re-lifts from the profile, which is byte-identical only because inference is a deterministic heuristic today. The real guarantee is this: the **approved ontology is frozen to a content-addressed artifact**, and regeneration loads that artifact and re-maps + re-codegens — the inference seam is never re-entered. The guarantee therefore survives an LLM entering inference.
- Frozen-artifact SHA-256: `624eec00c8f3d1f7ad508350ad48eda12ed5e571cfe142061f8b5f9b82bd8c9b`
- 5 regenerations from the frozen artifact → 1 distinct digest(s); matches the live generation: True.
- Inference re-entered on the regen path: **False** (enforced by `assert_no_inference`).
- Tampered-frozen-artifact rejected by integrity check: True.

## 3 · Confidence calibration (hard columns)

| column | category | expected | got | ✓ | conf | floor | ≥floor |
|--------|----------|----------|-----|---|------|-------|--------|
| app_user.ssn_last4 | pii_fixed_width | VARCHAR(4) | VARCHAR(4) | ✓ | 0.95 | 0.95 | ✓ |
| payment.card_last4 | pii_fixed_width | VARCHAR(4) | VARCHAR(4) | ✓ | 0.95 | 0.95 | ✓ |
| api_token.token_hash | fixed_width_identifier | VARCHAR(64) | VARCHAR(64) | ✓ | 0.95 | 0.95 | ✓ |
| app_user.last_login_at | timestamp_tz_distinction | TIMESTAMP_NTZ | TIMESTAMP_NTZ | ✓ | 0.9 | 0.9 | ✓ |
| org.created_at | timestamp_tz_distinction | TIMESTAMP_TZ | TIMESTAMP_TZ | ✓ | 0.9 | 0.9 | ✓ |
| invoice.total_amount | currency_precision | NUMBER(14,4) | NUMBER(14,4) | ✓ | 0.98 | 0.98 | ✓ |
| invoice.tax_rate | rate_precision | NUMBER(9,6) | NUMBER(9,6) | ✓ | 0.96 | 0.95 | ✓ |
| subscription.mrr_cents | currency_as_integer | NUMBER(38,0) | NUMBER(38,0) | ✓ | 0.95 | 0.95 | ✓ |
| plan.monthly_price | currency_precision | NUMBER(12,2) | NUMBER(12,2) | ✓ | 0.98 | 0.95 | ✓ |
| product.weight_kg | lossy_float | FLOAT | FLOAT | ✓ | 0.7 | 0.7 | ✓ |
| order_item.discount_pct | rate_precision | NUMBER(5,4) | NUMBER(5,4) | ✓ | 0.96 | 0.9 | ✓ |
| customer_order.shipping_address | semi_structured | VARIANT | VARIANT | ✓ | 0.85 | 0.85 | ✓ |
| product.attributes | semi_structured | VARIANT | VARIANT | ✓ | 0.85 | 0.85 | ✓ |
| customer_order.tags | array | ARRAY | ARRAY | ✓ | 0.85 | 0.85 | ✓ |
| product.dimensions_cm | array_numeric | ARRAY | ARRAY | ✓ | 0.8 | 0.8 | ✓ |
| audit_log.ip_address | no_native_target | VARCHAR(45) | VARCHAR(45) | ✓ | 0.85 | 0.85 | ✓ |
| customer_order.legacy_total_text | ambiguous_text_numeric | VARCHAR(32) | VARCHAR(32) | ✓ | 0.8 | 0.8 | ✓ |
| app_user.email | pii_text | VARCHAR(320) | VARCHAR(320) | ✓ | 0.9 | 0.9 | ✓ |
| warehouse.location_lat | lossy_float | FLOAT | FLOAT | ✓ | 0.75 | 0.75 | ✓ |
| app_user.role | enum | VARCHAR + CHECK or reference table | VARCHAR(64) | ✓ | 0.85 | 0.85 | ✓ |

## 4 · Hard-fail / trap detection (ablation)

A naive mapper was forced into each documented trap; the loop must catch every one via checksum/exec-error **or** the structural semantic guard (which defeats reconciliation-masking).

| column | naive target | checksum/exec | semantic guard | caught |
|--------|--------------|---------------|----------------|--------|
| app_user.ssn_last4 | NUMBER(38,0) | ✓ | ✓ | ✅ |
| payment.card_last4 | NUMBER(38,0) | ✓ | ✓ | ✅ |
| api_token.token_hash | NUMBER(38,0) | ✓ | ✓ | ✅ |
| app_user.last_login_at | TIMESTAMP_TZ | · | ✓ | ✅ |
| invoice.total_amount | FLOAT | ✓ | ✓ | ✅ |
| invoice.tax_rate | FLOAT | ✓ | ✓ | ✅ |
| subscription.mrr_cents | FLOAT | ✓ | ✓ | ✅ |
| plan.monthly_price | FLOAT | ✓ | ✓ | ✅ |
| order_item.discount_pct | FLOAT | ✓ | ✓ | ✅ |
| customer_order.shipping_address | VARCHAR(16777216) | ✓ | ✓ | ✅ |
| product.attributes | VARCHAR(16777216) | · | ✓ | ✅ |
| customer_order.tags | VARCHAR(16777216) | ✓ | ✓ | ✅ |
| product.dimensions_cm | VARCHAR(16777216) | · | ✓ | ✅ |
| audit_log.ip_address | NUMBER(38,0) | ✓ | · | ✅ |
| customer_order.legacy_total_text | NUMBER(38,0) | ✓ | ✓ | ✅ |

No semantic-guard false alarms on the correct mapping (correct run is clean — nothing is hidden).

## Caveats — what Phase 1 must still prove

This is a Phase-0 de-risk spike. The result is **necessary but not sufficient** for production; surfaced honestly so the go/no-go is not over-read:
- **No live Snowflake.** The target dialect is *compile-checked* with SQLGlot's Snowflake parser, and the medallion is *executed* on DuckDB as a stand-in. Phase 1 must re-run the identical generated Snowflake SQL on a real Snowflake account (warehouse sizing, micro-partition behavior, COPY-INTO load semantics).
- **VARIANT/ARRAY** are stored as canonical JSON text in the DuckDB proxy; their *content* preservation is reconciled and their VARIANT/ARRAY typing is compile-validated, but native semi-structured behavior is a Phase-1 confirmation.
- **Sample, not full volume.** Reconciliation ran on the seeded sample (≥15 tables, all documented traps). Phase 1 must reconcile at production volume and add scale/perf gates.
- **Inference pass is deterministic heuristic** here (reproducible, calibratable). The seam is LLM-pluggable; if an LLM is introduced it stays isolated to semantic-type inference only — never SQL generation (the reliability lever).

## Verdict

**✅ GO** for the §8 gate. All four technical criteria pass on the ≥15-table schema; feeds the CTO Phase-1 go/no-go.
