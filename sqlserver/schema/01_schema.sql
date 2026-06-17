-- =============================================================================
-- Phase 1 SQL Server test schema (SAU-323) — the INPUT side of the SQL Server
-- lane, the analogue of the Postgres rig (SAU-303). A realistic GL / policy /
-- claims / financial-transactions domain chosen to exercise the type traps that
-- break naive SQL Server -> Snowflake type mapping.
--
-- Design goals (per the §8 exit gate): >=15 tables, real foreign keys, and a
-- spread of deliberate type traps that are SPECIFIC to SQL Server:
--   * MONEY / SMALLMONEY      (fixed 4-dp scale silently dropped by default CONVERT)
--   * DATETIME vs DATETIME2   (3.33 ms legacy rounding vs 100-ns precision)
--   * DATETIMEOFFSET          (tz-aware instant; offset flips)
--   * UNIQUEIDENTIFIER        (GUID identity; must stay textual, not coerced)
--   * NVARCHAR width/collation (unicode + case-sensitive collation)
--   * ROWVERSION              (auto binary(8); the legacy name "timestamp" is a trap)
--   * implicit DECIMAL precision (high-scale rates)
-- plus the Postgres taxonomy adapted to SQL Server: currency-as-FLOAT, tz flips,
-- fixed-width PII coercion, precision loss, JSON-as-NVARCHAR flattening, and
-- numeric-looking text.
--
-- The seeded "hard columns" (02_seed.sql) plus the ground-truth manifest
-- (ground_truth/hard_columns.sqlserver.json) are the calibration target for the
-- core verification loop (consumed by SAU-321 children 3 & 4).
--
-- Idempotent: safe to re-run (drops the tables first, child-to-parent order).
-- Target: SQL Server 2019+/2022 (T-SQL). Database created by scripts/setup.sh.
-- =============================================================================

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

-- Drop in reverse-dependency order so re-runs are clean. ----------------------
DROP TABLE IF EXISTS dbo.audit_trail;
DROP TABLE IF EXISTS dbo.document;
DROP TABLE IF EXISTS dbo.exchange_rate;
DROP TABLE IF EXISTS dbo.transaction_ledger;
DROP TABLE IF EXISTS dbo.payment_method;
DROP TABLE IF EXISTS dbo.claim_payment;
DROP TABLE IF EXISTS dbo.claim;
DROP TABLE IF EXISTS dbo.coverage;
DROP TABLE IF EXISTS dbo.policy;
DROP TABLE IF EXISTS dbo.product_catalog;
DROP TABLE IF EXISTS dbo.agent;
DROP TABLE IF EXISTS dbo.policyholder;
DROP TABLE IF EXISTS dbo.journal_line;
DROP TABLE IF EXISTS dbo.journal_entry;
DROP TABLE IF EXISTS dbo.fiscal_period;
DROP TABLE IF EXISTS dbo.gl_account;
DROP TABLE IF EXISTS dbo.company;
GO

-- 1. company — tenant root. UNIQUEIDENTIFIER PK + tz-aware created_at + rowversion.
CREATE TABLE dbo.company (
  id            UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,   -- trap: GUID -> VARCHAR(36), never coerced/reformatted
  name          NVARCHAR(255)    NOT NULL,
  domain        VARCHAR(255)     NULL UNIQUE,
  is_active     BIT              NOT NULL DEFAULT 1,
  metadata_json NVARCHAR(MAX)    NULL CHECK (metadata_json IS NULL OR ISJSON(metadata_json) = 1),  -- trap: JSON-in-string -> VARIANT
  created_at    DATETIMEOFFSET(7) NOT NULL,              -- trap: tz-aware -> TIMESTAMP_TZ (offset must round-trip)
  row_version   ROWVERSION       NOT NULL                -- trap: auto binary(8); "timestamp" name lies -> BINARY(8), NOT a timestamp
);
GO

-- 2. gl_account — chart of accounts. Fixed-width code + CHECK-enforced domain.
CREATE TABLE dbo.gl_account (
  id            BIGINT IDENTITY(1,1) PRIMARY KEY,
  company_id    UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.company(id),
  account_code  CHAR(8)       NOT NULL,                  -- fixed-width, leading zeros ('00001000')
  name          NVARCHAR(200) NOT NULL,
  account_type  VARCHAR(12)   NOT NULL
                CHECK (account_type IN ('asset','liability','equity','revenue','expense')),  -- no native enum -> VARCHAR + CHECK
  is_active     BIT           NOT NULL DEFAULT 1,
  CONSTRAINT uq_gl_account UNIQUE (company_id, account_code)
);
GO

-- 3. fiscal_period — accounting periods.
CREATE TABLE dbo.fiscal_period (
  id          BIGINT IDENTITY(1,1) PRIMARY KEY,
  company_id  UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.company(id),
  period_code CHAR(6)     NOT NULL,                       -- 'YYYYMM' fixed-width
  starts_on   DATE        NOT NULL,
  ends_on     DATE        NOT NULL,
  status      VARCHAR(10) NOT NULL DEFAULT 'open'
              CHECK (status IN ('open','closed','locked')),
  CONSTRAINT uq_fiscal_period UNIQUE (company_id, period_code)
);
GO

-- 4. journal_entry — GL header. DATETIME (legacy) vs DATETIME2 (100-ns) vs DTO.
CREATE TABLE dbo.journal_entry (
  id             UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
  company_id     UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.company(id),
  period_id      BIGINT        NOT NULL REFERENCES dbo.fiscal_period(id),
  memo           NVARCHAR(500) NULL,
  source_ref     VARCHAR(40)   NULL,
  entered_at     DATETIME      NOT NULL,                  -- trap: legacy DATETIME ~3.33ms rounding -> TIMESTAMP_NTZ(3), NOT promoted to higher precision
  posted_at      DATETIME2(7)  NOT NULL,                  -- trap: 100-ns precision -> TIMESTAMP_NTZ(7), must NOT be truncated to ms
  created_offset DATETIMEOFFSET(7) NOT NULL               -- tz-aware companion
);
GO

-- 5. journal_line — debits/credits in MONEY.
CREATE TABLE dbo.journal_line (
  id         BIGINT IDENTITY(1,1) PRIMARY KEY,
  entry_id   UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.journal_entry(id),
  account_id BIGINT        NOT NULL REFERENCES dbo.gl_account(id),
  debit      MONEY         NOT NULL DEFAULT 0,            -- trap: MONEY is scale-4; default CONVERT/2-dp mapping silently drops 4th dp
  credit     MONEY         NOT NULL DEFAULT 0,
  currency   CHAR(3)       NOT NULL DEFAULT 'USD',
  memo       NVARCHAR(200) NULL
);
GO

-- 6. policyholder — PII-heavy. Unicode names, fixed-width PII, email length boundary.
CREATE TABLE dbo.policyholder (
  id              BIGINT IDENTITY(1,1) PRIMARY KEY,
  company_id      UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.company(id),
  full_name       NVARCHAR(200) NULL,                     -- trap: NVARCHAR unicode (CJK/diacritics) must survive; collation
  email           NVARCHAR(320) NOT NULL,                 -- PII; RFC 5321 max length boundary
  phone           VARCHAR(32)   NULL,                     -- PII, free-form
  ssn_last4       CHAR(4)       NULL,                      -- trap: fixed-width PII, leading zeros — must NOT become INT
  date_of_birth   DATE          NULL,                      -- PII
  preferences_json NVARCHAR(MAX) NULL CHECK (preferences_json IS NULL OR ISJSON(preferences_json) = 1),
  created_at      DATETIMEOFFSET(7) NOT NULL,
  CONSTRAINT uq_policyholder UNIQUE (company_id, email)
);
GO

-- 7. agent — selling agents; high-scale commission rate.
CREATE TABLE dbo.agent (
  id              BIGINT IDENTITY(1,1) PRIMARY KEY,
  company_id      UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.company(id),
  name            NVARCHAR(200) NOT NULL,
  license_no      VARCHAR(40)   NOT NULL UNIQUE,
  commission_rate DECIMAL(9,6)  NOT NULL DEFAULT 0,        -- precision-sensitive rate
  hired_on        DATE          NULL
);
GO

-- 8. product_catalog — insurance products; REAL rate factor (lossy) + JSON attrs.
CREATE TABLE dbo.product_catalog (
  id               BIGINT IDENTITY(1,1) PRIMARY KEY,
  code             VARCHAR(40)   NOT NULL UNIQUE,
  name             NVARCHAR(200) NOT NULL,
  description      NVARCHAR(MAX) NULL,                     -- unbounded text -> VARCHAR(max)
  base_rate_factor REAL          NULL,                     -- trap: REAL (float4) lossy; FLOAT acceptable but must flag lossy
  attributes_json  NVARCHAR(MAX) NULL CHECK (attributes_json IS NULL OR ISJSON(attributes_json) = 1)  -- trap: JSON -> VARIANT
);
GO

-- 9. policy — core operational entity. MONEY premium + SMALLMONEY fee + DTO + numeric-text.
CREATE TABLE dbo.policy (
  id                  UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
  company_id          UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.company(id),
  policyholder_id     BIGINT        NOT NULL REFERENCES dbo.policyholder(id),
  agent_id            BIGINT        NULL REFERENCES dbo.agent(id),
  product_id          BIGINT        NOT NULL REFERENCES dbo.product_catalog(id),
  policy_number       CHAR(12)      NOT NULL UNIQUE,        -- fixed-width identifier
  status              VARCHAR(12)   NOT NULL DEFAULT 'quoted'
                      CHECK (status IN ('quoted','bound','active','lapsed','canceled')),
  premium             MONEY         NOT NULL,               -- trap: MONEY scale-4 currency -> NUMBER(19,4), NOT FLOAT / NOT 2-dp
  policy_fee          SMALLMONEY    NOT NULL DEFAULT 0,      -- trap: SMALLMONEY scale-4 -> NUMBER(10,4)
  effective_at        DATETIMEOFFSET(7) NOT NULL,           -- trap: tz-aware -> TIMESTAMP_TZ (offset flip corrupts instant)
  expires_on          DATE          NULL,
  legacy_premium_text VARCHAR(32)   NULL                    -- trap: numeric-LOOKING text ('1234.5600','1.234,56') — must NOT auto-parse
);
GO

-- 10. coverage — per-policy coverage lines. DECIMAL limits + MONEY deductible.
CREATE TABLE dbo.coverage (
  id              BIGINT IDENTITY(1,1) PRIMARY KEY,
  policy_id       UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.policy(id),
  coverage_type   VARCHAR(40)   NOT NULL,
  limit_amount    DECIMAL(18,2) NOT NULL,                  -- trap: currency precision -> NUMBER(18,2), NOT FLOAT
  deductible      MONEY         NOT NULL DEFAULT 0,
  premium_portion SMALLMONEY    NOT NULL DEFAULT 0
);
GO

-- 11. claim — claims. MONEY amount + FLOAT reserve (currency-as-FLOAT trap) + DATETIME2 + DTO + JSON.
CREATE TABLE dbo.claim (
  id             UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
  company_id     UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.company(id),
  policy_id      UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.policy(id),
  claim_number   CHAR(14)      NOT NULL UNIQUE,             -- fixed-width identifier
  status         VARCHAR(12)   NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','review','approved','denied','paid','closed')),
  claim_amount   MONEY         NOT NULL,                    -- MONEY scale-4
  reserve_amount FLOAT         NULL,                        -- trap: money stored as FLOAT(53) — lossy; must FLAG, never assert byte-exact currency
  reported_at    DATETIME2(7)  NOT NULL,                    -- trap: 100-ns precision
  incident_at    DATETIMEOFFSET(7) NULL,                    -- tz-aware
  metadata_json  NVARCHAR(MAX) NULL CHECK (metadata_json IS NULL OR ISJSON(metadata_json) = 1),  -- trap: JSON unicode/emoji -> VARIANT
  description    NVARCHAR(MAX) NULL
);
GO

-- 12. claim_payment — disbursements. MONEY amount + SMALLMONEY fee.
CREATE TABLE dbo.claim_payment (
  id            BIGINT IDENTITY(1,1) PRIMARY KEY,
  claim_id      UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.claim(id),
  amount        MONEY        NOT NULL,
  fee           SMALLMONEY   NOT NULL DEFAULT 0,            -- trap: SMALLMONEY scale-4 fee (1.9900) -> NUMBER(10,4)
  method        VARCHAR(8)   NOT NULL DEFAULT 'ach'
                CHECK (method IN ('ach','wire','check','card')),
  processor_ref VARCHAR(80)  NULL,
  paid_at       DATETIME2(7) NOT NULL
);
GO

-- 13. payment_method — stored instruments. Fixed-width PII (last4).
CREATE TABLE dbo.payment_method (
  id              BIGINT IDENTITY(1,1) PRIMARY KEY,
  policyholder_id BIGINT       NOT NULL REFERENCES dbo.policyholder(id),
  kind            VARCHAR(8)   NOT NULL
                  CHECK (kind IN ('card','ach','wire')),
  card_last4      CHAR(4)      NULL,                        -- trap: fixed-width PII, leading zeros
  routing_last4   CHAR(4)      NULL,
  bank_name       NVARCHAR(120) NULL,
  created_at      DATETIMEOFFSET(7) NOT NULL
);
GO

-- 14. transaction_ledger — financial transactions. MONEY + high-scale FX rate.
CREATE TABLE dbo.transaction_ledger (
  id            BIGINT IDENTITY(1,1) PRIMARY KEY,
  company_id    UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.company(id),
  account_id    BIGINT        NOT NULL REFERENCES dbo.gl_account(id),
  amount        MONEY         NOT NULL,                     -- MONEY scale-4
  currency      CHAR(3)       NOT NULL DEFAULT 'USD',
  exchange_rate DECIMAL(18,8) NOT NULL DEFAULT 1,           -- trap: 8-dp FX rate — float rounding corrupts the 8th place
  fx_amount     DECIMAL(19,4) NOT NULL,
  kind          VARCHAR(16)   NOT NULL DEFAULT 'posting',
  occurred_at   DATETIMEOFFSET(7) NOT NULL
);
GO

-- 15. exchange_rate — currency rate table. High-scale DECIMAL.
CREATE TABLE dbo.exchange_rate (
  id        BIGINT IDENTITY(1,1) PRIMARY KEY,
  base_ccy  CHAR(3)       NOT NULL,
  quote_ccy CHAR(3)       NOT NULL,
  rate      DECIMAL(18,8) NOT NULL,                         -- 8-dp rate
  as_of     DATE          NOT NULL,
  CONSTRAINT uq_exchange_rate UNIQUE (base_ccy, quote_ccy, as_of)
);
GO

-- 16. document — attachments. Fixed-width hash + case-sensitive collation column.
CREATE TABLE dbo.document (
  id          BIGINT IDENTITY(1,1) PRIMARY KEY,
  company_id  UNIQUEIDENTIFIER NOT NULL REFERENCES dbo.company(id),
  claim_id    UNIQUEIDENTIFIER NULL REFERENCES dbo.claim(id),  -- nullable FK
  file_name   NVARCHAR(255) NOT NULL,
  file_hash   CHAR(64)      NOT NULL,                        -- trap: fixed-width hex (sha-256), leading zeros — must stay textual
  mime_type   VARCHAR(80)   NULL,
  title_cs    NVARCHAR(200) COLLATE Latin1_General_CS_AS NULL,  -- trap: case-sensitive collation; case must be preserved
  uploaded_at DATETIME2(7)  NOT NULL
);
GO

-- 17. audit_trail — security-adjacent. inet-style text + JSON + rowversion.
CREATE TABLE dbo.audit_trail (
  id          UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
  actor_user  NVARCHAR(200) NULL,                          -- nullable
  action      VARCHAR(60)   NOT NULL,
  entity_type VARCHAR(60)   NOT NULL,
  entity_id   VARCHAR(64)   NULL,
  ip_address  VARCHAR(45)   NULL,                           -- trap: IPv4 + IPv6 ('::1') text; no native target -> VARCHAR(45)
  payload_json NVARCHAR(MAX) NULL CHECK (payload_json IS NULL OR ISJSON(payload_json) = 1),
  occurred_at DATETIME2(7)  NOT NULL,
  row_version ROWVERSION    NOT NULL                        -- trap: auto binary(8)
);
GO

-- Helpful FK indexes (also signal relationships to the profiler). -------------
CREATE INDEX idx_gl_account_company    ON dbo.gl_account(company_id);
CREATE INDEX idx_fiscal_period_company ON dbo.fiscal_period(company_id);
CREATE INDEX idx_journal_entry_company ON dbo.journal_entry(company_id);
CREATE INDEX idx_journal_entry_period  ON dbo.journal_entry(period_id);
CREATE INDEX idx_journal_line_entry    ON dbo.journal_line(entry_id);
CREATE INDEX idx_journal_line_account  ON dbo.journal_line(account_id);
CREATE INDEX idx_policyholder_company  ON dbo.policyholder(company_id);
CREATE INDEX idx_agent_company         ON dbo.agent(company_id);
CREATE INDEX idx_policy_company        ON dbo.policy(company_id);
CREATE INDEX idx_policy_holder         ON dbo.policy(policyholder_id);
CREATE INDEX idx_policy_product        ON dbo.policy(product_id);
CREATE INDEX idx_coverage_policy       ON dbo.coverage(policy_id);
CREATE INDEX idx_claim_company         ON dbo.claim(company_id);
CREATE INDEX idx_claim_policy          ON dbo.claim(policy_id);
CREATE INDEX idx_claim_payment_claim   ON dbo.claim_payment(claim_id);
CREATE INDEX idx_payment_method_holder ON dbo.payment_method(policyholder_id);
CREATE INDEX idx_txn_ledger_company    ON dbo.transaction_ledger(company_id);
CREATE INDEX idx_txn_ledger_account    ON dbo.transaction_ledger(account_id);
CREATE INDEX idx_document_company      ON dbo.document(company_id);
CREATE INDEX idx_document_claim        ON dbo.document(claim_id);
GO
