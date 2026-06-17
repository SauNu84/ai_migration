# SQL Server trap corpus — expected-catch manifest (SAU-323)

This is the human-readable companion to
[`hard_columns.sqlserver.json`](hard_columns.sqlserver.json). Each trap below names
**which column** carries it, **what a naive mapper does wrong**, the **seed value**
that exercises it, and the **§8 detector expected to catch** the naive mapping.

Two §8 detectors are referenced (same as the Postgres lane):

- **checksum** — data reconciliation: row-count + per-column checksum on Gold.
  Catches traps where the naive mapping produces a *different value* on real data.
- **semantic_guard** — the structural type-semantics guard. Catches traps where the
  type *semantics* change (tz-awareness, scale, text→numeric, JSON/array flattening,
  binary-vs-timestamp) **even when the sample data coincidentally round-trips**.

A correct mapping must trip **neither** detector — that is the "no false alarm" half
of the §8 hard-fail bullet.

---

## 1. MONEY / SMALLMONEY — the silent 4-dp drop

SQL Server `MONEY` and `SMALLMONEY` are **fixed scale-4**, but the default
`CONVERT(varchar, money)` emits only **2 dp**, and the common "money is dollars and
cents" instinct maps them to `NUMBER(_,2)` or `FLOAT` — silently dropping the 4th dp.

| Column | T-SQL | Correct target | Trap value | Detect |
|---|---|---|---|---|
| `policy.premium` | `money` | `NUMBER(19,4)` | `999999.9999`, `0.0001` | checksum + semantic_guard |
| `journal_line.debit` | `money` | `NUMBER(19,4)` | `0.0001` sub-cent plug | checksum + semantic_guard |
| `claim_payment.fee` | `smallmoney` | `NUMBER(10,4)` | `0.0001` | checksum + semantic_guard |

**Proves:** the loop preserves the 4th decimal of money types and never narrows to
2-dp or float.

## 2. DATETIME vs DATETIME2 — legacy rounding vs 100-ns precision

`DATETIME` rounds to ~3.33 ms ticks (1/300 s); `DATETIME2(7)` keeps 100-ns. Mapping
`DATETIME2` down to ms (or to legacy `DATETIME`) truncates; mapping `DATETIME` up to
7-dp invents precision.

| Column | T-SQL | Correct target | Trap value | Detect |
|---|---|---|---|---|
| `journal_entry.entered_at` | `datetime` | `TIMESTAMP_NTZ(3)` | `…08:15:00.123` (stored `.123333`) | checksum + semantic_guard |
| `journal_entry.posted_at` | `datetime2(7)` | `TIMESTAMP_NTZ(7)` | `…59.9999999` | checksum + semantic_guard |

**Proves:** sub-microsecond digits survive, and the legacy tick is reproduced rather
than promoted.

## 3. DATETIMEOFFSET — tz flips

`DATETIMEOFFSET` is a tz-aware instant. Dropping the offset (→ `TIMESTAMP_NTZ`) or
flipping it shifts the instant. The seed uses `+00`, `+02`, `+09`, and a `-05` claim
incident.

| Column | T-SQL | Correct target | Trap value | Detect |
|---|---|---|---|---|
| `policy.effective_at` | `datetimeoffset(7)` | `TIMESTAMP_TZ` | `2024-02-29 12:00:00 +09:00` | checksum + semantic_guard |
| `company.created_at` | `datetimeoffset(7)` | `TIMESTAMP_TZ` | `+00 / +02 / +09` | checksum + semantic_guard |

**Proves:** the same UTC instant + offset round-trips; no naive wall-clock NTZ.

## 4. UNIQUEIDENTIFIER — GUID identity

No Snowflake native GUID type. Must serialize to canonical 36-char text. Coercing to
binary/number, re-casing, or adding braces breaks FK join identity.

| Column | T-SQL | Correct target | Trap value | Detect |
|---|---|---|---|---|
| `company.id` | `uniqueidentifier` | `VARCHAR(36)` | `11111111-…` (FK'd by 6 tables) | checksum + semantic_guard |

**Proves:** GUID stays canonical text and joins still resolve.

## 5. ROWVERSION — the "timestamp" that isn't

`ROWVERSION` (legacy synonym `timestamp`) is an auto 8-byte **binary** counter, not a
datetime. The name lures a naive mapper into `TIMESTAMP_*`; it must map to `BINARY(8)`.

| Column | T-SQL | Correct target | Trap | Detect |
|---|---|---|---|---|
| `audit_trail.row_version` | `rowversion` | `BINARY(8)` | name-driven mismapping to a timestamp | semantic_guard |
| `company.row_version` | `rowversion` | `BINARY(8)` | (same) | semantic_guard |

**Proves:** binary identity preserved; not interpreted as time.

## 6. NVARCHAR width & collation — unicode + case sensitivity

| Column | T-SQL | Correct target | Trap value | Detect |
|---|---|---|---|---|
| `policyholder.full_name` | `nvarchar(200)` | `VARCHAR(200)` | `田中 太郎`, `Carl Müller` | checksum + semantic_guard |
| `document.title_cs` | `nvarchar COLLATE Latin1_General_CS_AS` | `VARCHAR` (case preserved) | `Estimate` vs `ESTIMATE` | checksum |

**Proves:** unicode survives a non-unicode codepage, and case-sensitive values are not
folded.

## 7. Currency-as-FLOAT — the lossy-money smell

Money held in `FLOAT(53)` cannot represent 4-dp currency exactly. `FLOAT→FLOAT` is
*acceptable*, but the mapper must **flag it lossy** and report **low confidence** —
not assert exact-scale currency equality.

| Column | T-SQL | Correct target | Trap value | Detect |
|---|---|---|---|---|
| `claim.reserve_amount` | `float` | `FLOAT` (flagged lossy) | `499999.9999999` | semantic_guard |

**Proves:** the loop distinguishes "exact money" from "money-in-float" and lowers
confidence (floor 0.70) instead of over-claiming.

## 8. Precision loss — high-scale DECIMAL

| Column | T-SQL | Correct target | Trap value | Detect |
|---|---|---|---|---|
| `transaction_ledger.exchange_rate` | `decimal(18,8)` | `NUMBER(18,8)` | `0.00667125` | checksum + semantic_guard |
| `coverage.limit_amount` | `decimal(18,2)` | `NUMBER(18,2)` | `10000000.00` | checksum + semantic_guard |
| `agent.commission_rate` | `decimal(9,6)` | `NUMBER(9,6)` | `0.083325` | checksum + semantic_guard |

**Proves:** the 8th / 6th decimal of rates and large-magnitude scale-2 currency hold.

## 9. Fixed-width PII coercion — leading zeros

| Column | T-SQL | Correct target | Trap value | Detect |
|---|---|---|---|---|
| `policyholder.ssn_last4` | `char(4)` | `VARCHAR(4)` | `0042`, `0007` | checksum + semantic_guard |
| `payment_method.card_last4` | `char(4)` | `VARCHAR(4)` | `0019` | checksum + semantic_guard |
| `document.file_hash` | `char(64)` | `VARCHAR(64)` | `0000…abc` | checksum + semantic_guard |

**Proves:** numeric coercion (which eats leading zeros) is rejected; identity is text.

## 10. JSON flattening — NVARCHAR(MAX) → VARIANT

SQL Server has no JSON type; JSON lives in `NVARCHAR(MAX)` (guarded by `ISJSON`).
Flattening to a plain `VARCHAR` loses structure/queryability.

| Column | T-SQL | Correct target | Trap value | Detect |
|---|---|---|---|---|
| `product_catalog.attributes_json` | `nvarchar(max)` | `VARIANT` | nested object | semantic_guard |
| `claim.metadata_json` | `nvarchar(max)` | `VARIANT` | `🔥`, `Küche`, `テスト` | semantic_guard |

**Proves:** JSON is parsed into VARIANT with unicode/emoji intact, not flattened.

## 11. No native target — IP text

| Column | T-SQL | Correct target | Trap value | Detect |
|---|---|---|---|---|
| `audit_trail.ip_address` | `varchar(45)` | `VARCHAR(45)` | `::1`, `2001:db8::8a2e:370:7334` | checksum |

**Proves:** IPv4 + IPv6 text serialize losslessly (no reparsing/normalization).

## 12. Ambiguous text-numeric — do not auto-parse

| Column | T-SQL | Correct target | Trap value | Detect |
|---|---|---|---|---|
| `policy.legacy_premium_text` | `varchar(32)` | `VARCHAR(32)` | `899,50` (comma decimal) | semantic_guard |

**Proves:** numeric-looking text stays text; `899,50` is not silently turned into a
number (which would corrupt it).

---

### Coverage summary

20 calibration columns, spanning every SQL-Server trap class named in the issue —
`MONEY`/`SMALLMONEY`, `DATETIME` vs `DATETIME2`, `DATETIMEOFFSET`,
`UNIQUEIDENTIFIER`, `NVARCHAR` width/collation, `rowversion`, implicit DECIMAL
precision — plus the mirrored Postgres taxonomy (currency-as-FLOAT, tz flips, PII
coercion, precision loss, JSON flattening, ambiguous text). The CHECK-enforced
`account_type` / `status` columns additionally exercise the "no native enum" mapping
(VARCHAR + CHECK / ref table) on the SQL Server side.
