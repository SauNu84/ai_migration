-- ─────────────────────────────────────────────────────────────────────────────
-- Phase 0 test schema (SAU-303) — the INPUT side of the Postgres→Snowflake
-- verification spike (SAU-302). A realistic B2B SaaS billing/CRM domain chosen to
-- exercise the type edge cases that break naive type mapping.
--
-- Design goals (per the §8 exit gate): ≥15 tables, real foreign keys, and a spread
-- of deliberate type traps — precision-sensitive numerics, tz vs non-tz timestamps,
-- JSON/array/enum/inet columns, wide varchars, fixed-width identifiers, and
-- numeric-looking text. The seeded "hard columns" (02_seed.sql) plus the ground-truth
-- manifest (ground_truth/hard_columns.json) are the calibration target for the core
-- verification loop (child issue #2).
--
-- Idempotent: safe to re-run (drops the schema objects first).
-- ─────────────────────────────────────────────────────────────────────────────

DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
SET search_path TO public;

-- ── Enums (map to Snowflake VARCHAR + a CHECK/ref table — a mapping decision) ────
CREATE TYPE user_role          AS ENUM ('owner', 'admin', 'member', 'billing', 'readonly');
CREATE TYPE subscription_status AS ENUM ('trialing', 'active', 'past_due', 'canceled', 'paused');
CREATE TYPE order_status        AS ENUM ('cart', 'placed', 'paid', 'fulfilled', 'refunded', 'canceled');
CREATE TYPE payment_method      AS ENUM ('card', 'ach', 'wire', 'paypal', 'credit');
CREATE TYPE ticket_priority     AS ENUM ('low', 'normal', 'high', 'urgent');
CREATE TYPE currency_code       AS ENUM ('USD', 'EUR', 'GBP', 'JPY', 'VND');

-- 1. org — tenant root.
CREATE TABLE org (
  id          BIGSERIAL PRIMARY KEY,
  name        VARCHAR(255) NOT NULL,
  domain      VARCHAR(255) UNIQUE,
  is_active   BOOLEAN NOT NULL DEFAULT true,
  metadata    JSONB,                              -- trap: JSONB → VARIANT
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()  -- tz-aware → TIMESTAMP_TZ
);

-- 2. plan — product plans with precise pricing.
CREATE TABLE plan (
  id            BIGSERIAL PRIMARY KEY,
  code          VARCHAR(40) NOT NULL UNIQUE,
  name          VARCHAR(120) NOT NULL,
  monthly_price NUMERIC(12,2) NOT NULL,           -- trap: currency precision → NUMBER(12,2), NOT FLOAT
  currency      currency_code NOT NULL DEFAULT 'USD',
  features      TEXT[] NOT NULL DEFAULT '{}'      -- trap: array → ARRAY
);

-- 3. app_user — PII-heavy.
CREATE TABLE app_user (
  id            BIGSERIAL PRIMARY KEY,
  org_id        BIGINT NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  email         VARCHAR(320) NOT NULL,            -- PII
  full_name     VARCHAR(200),                     -- PII, nullable
  role          user_role NOT NULL DEFAULT 'member',
  phone         VARCHAR(32),                      -- PII, free-form
  ssn_last4     CHAR(4),                          -- trap: fixed-width, leading zeros — must NOT become INT
  preferences   JSONB,
  last_login_at TIMESTAMP,                        -- trap: NO tz → TIMESTAMP_NTZ (not TIMESTAMP_TZ)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (org_id, email)
);

-- 4. subscription
CREATE TABLE subscription (
  id            BIGSERIAL PRIMARY KEY,
  org_id        BIGINT NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  plan_id       BIGINT NOT NULL REFERENCES plan(id),
  status        subscription_status NOT NULL DEFAULT 'trialing',
  seats         INTEGER NOT NULL DEFAULT 1,
  mrr_cents     BIGINT NOT NULL DEFAULT 0,        -- trap: money-as-integer-cents → NUMBER(38,0), NOT FLOAT
  started_on    DATE NOT NULL,
  renews_at     TIMESTAMPTZ,
  trial_ends_at TIMESTAMPTZ
);

-- 5. invoice
CREATE TABLE invoice (
  id             BIGSERIAL PRIMARY KEY,
  org_id         BIGINT NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  subscription_id BIGINT REFERENCES subscription(id),
  number         VARCHAR(40) NOT NULL UNIQUE,
  total_amount   NUMERIC(14,4) NOT NULL,          -- trap: high-precision currency → NUMBER(14,4)
  tax_rate       NUMERIC(9,6) NOT NULL DEFAULT 0, -- trap: precision-sensitive rate
  currency       currency_code NOT NULL DEFAULT 'USD',
  status         VARCHAR(20) NOT NULL DEFAULT 'open',
  issued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  due_date       DATE,
  paid_at        TIMESTAMPTZ                       -- nullable
);

-- 6. invoice_line
CREATE TABLE invoice_line (
  id          BIGSERIAL PRIMARY KEY,
  invoice_id  BIGINT NOT NULL REFERENCES invoice(id) ON DELETE CASCADE,
  description VARCHAR(1000) NOT NULL,              -- wide varchar
  quantity    NUMERIC(12,3) NOT NULL,
  unit_price  NUMERIC(14,4) NOT NULL,
  amount      NUMERIC(14,4) NOT NULL,
  tax_code    VARCHAR(20)
);

-- 7. payment
CREATE TABLE payment (
  id           BIGSERIAL PRIMARY KEY,
  invoice_id   BIGINT NOT NULL REFERENCES invoice(id) ON DELETE CASCADE,
  amount_cents BIGINT NOT NULL,                   -- money-as-cents
  method       payment_method NOT NULL,
  processor_ref VARCHAR(80),
  card_last4   CHAR(4),                            -- trap: fixed-width PII, leading zeros
  raw_response JSONB,                              -- trap: JSONB → VARIANT
  captured_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 8. warehouse
CREATE TABLE warehouse (
  id           BIGSERIAL PRIMARY KEY,
  code         VARCHAR(20) NOT NULL UNIQUE,
  name         VARCHAR(120) NOT NULL,
  location_lat DOUBLE PRECISION,                  -- float → FLOAT
  location_lng DOUBLE PRECISION,
  tz_name      VARCHAR(40)
);

-- 9. product
CREATE TABLE product (
  id            BIGSERIAL PRIMARY KEY,
  sku           VARCHAR(60) NOT NULL UNIQUE,
  name          VARCHAR(200) NOT NULL,
  description   TEXT,                              -- unbounded text → VARCHAR(max)
  price         NUMERIC(12,2) NOT NULL,
  weight_kg     REAL,                              -- trap: REAL (float4) → lossy; ground truth flags risk
  dimensions_cm NUMERIC(8,2)[],                    -- trap: array of numerics
  attributes    JSONB                              -- trap: JSONB → VARIANT
);

-- 10. inventory
CREATE TABLE inventory (
  id           BIGSERIAL PRIMARY KEY,
  product_id   BIGINT NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  warehouse_id BIGINT NOT NULL REFERENCES warehouse(id) ON DELETE CASCADE,
  quantity     INTEGER NOT NULL DEFAULT 0,
  reorder_level INTEGER NOT NULL DEFAULT 0,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (product_id, warehouse_id)
);

-- 11. customer_order  ("order" is reserved; use a safe name)
CREATE TABLE customer_order (
  id                BIGSERIAL PRIMARY KEY,
  org_id            BIGINT NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  user_id           BIGINT REFERENCES app_user(id),
  status            order_status NOT NULL DEFAULT 'cart',
  placed_at         TIMESTAMPTZ,
  total             NUMERIC(14,4) NOT NULL DEFAULT 0,
  currency          currency_code NOT NULL DEFAULT 'USD',
  shipping_address  JSONB,                         -- trap: nested JSON → VARIANT/OBJECT
  tags              TEXT[] NOT NULL DEFAULT '{}',
  legacy_total_text VARCHAR(32)                    -- trap: numeric-looking TEXT (lossy-coercion landmine)
);

-- 12. order_item
CREATE TABLE order_item (
  id           BIGSERIAL PRIMARY KEY,
  order_id     BIGINT NOT NULL REFERENCES customer_order(id) ON DELETE CASCADE,
  product_id   BIGINT NOT NULL REFERENCES product(id),
  quantity     INTEGER NOT NULL,
  unit_price   NUMERIC(14,4) NOT NULL,
  discount_pct NUMERIC(5,4) NOT NULL DEFAULT 0     -- trap: scale-4 fraction (0.0000–1.0000)
);

-- 13. support_ticket
CREATE TABLE support_ticket (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  user_id     BIGINT REFERENCES app_user(id),
  priority    ticket_priority NOT NULL DEFAULT 'normal',
  subject     VARCHAR(500) NOT NULL,
  body        TEXT,
  sentiment   REAL,                                -- nullable float
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);

-- 14. audit_log
CREATE TABLE audit_log (
  id            BIGSERIAL PRIMARY KEY,
  actor_user_id BIGINT REFERENCES app_user(id),   -- nullable FK
  action        VARCHAR(60) NOT NULL,
  entity_type   VARCHAR(60) NOT NULL,
  entity_id     BIGINT,
  ip_address    INET,                              -- trap: INET has no Snowflake native type → VARCHAR
  payload       JSONB,
  occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 15. event_metric
CREATE TABLE event_metric (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  metric_name VARCHAR(80) NOT NULL,
  value       DOUBLE PRECISION NOT NULL,           -- float metric
  dimensions  JSONB,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 16. api_token  (security-adjacent; secret-like fixed-width + expiry edge cases)
CREATE TABLE api_token (
  id          BIGSERIAL PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  token_hash  CHAR(64) NOT NULL UNIQUE,            -- fixed-width hex (sha-256) — must stay CHAR/VARCHAR
  scopes      TEXT[] NOT NULL DEFAULT '{}',
  expires_at  TIMESTAMPTZ,
  revoked     BOOLEAN NOT NULL DEFAULT false,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Helpful FK indexes (also signal relationships to the profiler).
CREATE INDEX idx_app_user_org       ON app_user(org_id);
CREATE INDEX idx_subscription_org   ON subscription(org_id);
CREATE INDEX idx_invoice_org        ON invoice(org_id);
CREATE INDEX idx_invoice_line_inv   ON invoice_line(invoice_id);
CREATE INDEX idx_payment_invoice    ON payment(invoice_id);
CREATE INDEX idx_inventory_product  ON inventory(product_id);
CREATE INDEX idx_order_org          ON customer_order(org_id);
CREATE INDEX idx_order_item_order   ON order_item(order_id);
CREATE INDEX idx_ticket_org         ON support_ticket(org_id);
CREATE INDEX idx_audit_actor        ON audit_log(actor_user_id);
CREATE INDEX idx_metric_org         ON event_metric(org_id);
CREATE INDEX idx_api_token_org      ON api_token(org_id);
