-- ─────────────────────────────────────────────────────────────────────────────
-- Phase 0 seed (SAU-303) — deterministic, reproducible data that deliberately
-- exercises the hard columns documented in ground_truth/hard_columns.json.
--
-- The values here are the *ground truth*: every edge case (leading-zero fixed-width
-- ids, high-precision currency, tz vs non-tz timestamps, numeric-looking text,
-- nested JSON, arrays, inet, nulls, unicode) is chosen so the core verification loop
-- can prove byte/precision-exact round-trips, not just "it loaded".
-- ─────────────────────────────────────────────────────────────────────────────
SET search_path TO public;
SET TIME ZONE 'UTC';

-- orgs
INSERT INTO org (id, name, domain, is_active, metadata, created_at) VALUES
  (1, 'Acme Corp',        'acme.example',     true,  '{"tier":"enterprise","seats":250,"flags":["beta","sso"]}',          TIMESTAMPTZ '2023-01-15 09:30:00+00'),
  (2, 'Globex GmbH',      'globex.example',   true,  '{"tier":"growth","region":"eu","vat":"DE123456789"}',              TIMESTAMPTZ '2023-06-01 23:59:59+02'),  -- tz offset edge
  (3, 'Sōko K.K.',        'soko.example',     true,  '{"tier":"growth","locale":"ja-JP"}',                                TIMESTAMPTZ '2024-02-29 12:00:00+09'),  -- unicode + leap day + JST
  (4, 'Inactive Inc',     NULL,               false, NULL,                                                                 TIMESTAMPTZ '2022-11-30 00:00:00+00'); -- null domain + null metadata
SELECT setval('org_id_seq', 4);

-- plans — currency precision matters
INSERT INTO plan (id, code, name, monthly_price, currency, features) VALUES
  (1, 'free',        'Free',        0.00,     'USD', '{}'),
  (2, 'growth',      'Growth',      99.99,    'USD', ARRAY['seats:10','sso:false']),
  (3, 'enterprise',  'Enterprise',  2499.50,  'USD', ARRAY['sso','audit_log','priority_support']),
  (4, 'growth_eur',  'Growth (EU)', 89.95,    'EUR', ARRAY['seats:10']),
  (5, 'jpy_plan',    'JPY Plan',    12000.00, 'JPY', ARRAY['domestic']);  -- JPY: 2dp stored though currency is 0-dp (trap)
SELECT setval('plan_id_seq', 5);

-- app_user — PII + fixed-width leading zeros + non-tz last_login
INSERT INTO app_user (id, org_id, email, full_name, role, phone, ssn_last4, preferences, last_login_at, created_at) VALUES
  (1, 1, 'owner@acme.example',    'Alice Owner',   'owner',   '+1-415-555-0100', '0042', '{"theme":"dark","digest":"weekly"}', TIMESTAMP '2024-03-01 08:15:00', TIMESTAMPTZ '2023-01-15 09:31:00+00'),
  (2, 1, 'admin@acme.example',    'Bob Admin',     'admin',   '+1 (415) 555-0101', '0007', '{}',                                TIMESTAMP '2024-03-02 17:45:30', TIMESTAMPTZ '2023-01-16 10:00:00+00'),
  (3, 1, 'readonly@acme.example', NULL,            'readonly', NULL,             NULL,   NULL,                                 NULL,                              TIMESTAMPTZ '2023-02-01 00:00:00+00'),  -- many nulls
  (4, 2, 'chef@globex.example',   'Carl Müller',   'admin',   '+49 30 1234567',  '1000', '{"locale":"de-DE"}',                TIMESTAMP '2024-02-28 23:59:59', TIMESTAMPTZ '2023-06-01 22:00:00+00'),  -- unicode name
  (5, 3, 'tanaka@soko.example',   '田中 太郎',      'owner',   '+81-3-1234-5678', '0815', '{"locale":"ja-JP"}',                TIMESTAMP '2024-02-29 09:00:00', TIMESTAMPTZ '2024-02-29 03:00:00+00'); -- unicode + leap day
SELECT setval('app_user_id_seq', 5);

-- subscriptions — mrr as integer cents
INSERT INTO subscription (id, org_id, plan_id, status, seats, mrr_cents, started_on, renews_at, trial_ends_at) VALUES
  (1, 1, 3, 'active',   250, 24990000, DATE '2023-01-20', TIMESTAMPTZ '2025-01-20 00:00:00+00', NULL),       -- $249,900.00 in cents
  (2, 2, 4, 'active',    10,   899500, DATE '2023-06-05', TIMESTAMPTZ '2025-06-05 00:00:00+00', NULL),
  (3, 3, 2, 'trialing',   3,        0, DATE '2024-02-29', TIMESTAMPTZ '2025-02-28 00:00:00+00', TIMESTAMPTZ '2024-03-14 00:00:00+00'),
  (4, 4, 1, 'canceled',   1,        0, DATE '2022-12-01', NULL,                                  NULL);
SELECT setval('subscription_id_seq', 4);

-- invoices — high-precision totals + precision-sensitive tax_rate + null paid_at
INSERT INTO invoice (id, org_id, subscription_id, number, total_amount, tax_rate, currency, status, issued_at, due_date, paid_at) VALUES
  (1, 1, 1, 'INV-2024-0001', 24990.0000, 0.000000, 'USD', 'paid', TIMESTAMPTZ '2024-01-01 00:00:00+00', DATE '2024-01-31', TIMESTAMPTZ '2024-01-05 14:22:01+00'),
  (2, 2, 2, 'INV-2024-0002',   899.5000, 0.190000, 'EUR', 'paid', TIMESTAMPTZ '2024-01-01 00:00:00+00', DATE '2024-01-31', TIMESTAMPTZ '2024-01-10 09:00:00+00'),  -- 19% VAT
  (3, 3, 3, 'INV-2024-0003',     0.0001, 0.083325, 'JPY', 'open', TIMESTAMPTZ '2024-02-29 00:00:00+00', DATE '2024-03-31', NULL),                                  -- sub-cent + 6dp rate
  (4, 1, 1, 'INV-2024-0004', 99999999.9999, 0.075000, 'USD', 'open', TIMESTAMPTZ '2024-03-01 00:00:00+00', DATE '2024-03-31', NULL);                                 -- max-precision boundary
SELECT setval('invoice_id_seq', 4);

INSERT INTO invoice_line (invoice_id, description, quantity, unit_price, amount, tax_code) VALUES
  (1, 'Enterprise seats (annual)',                          250.000, 99.9600, 24990.0000, 'NONE'),
  (2, 'Growth EU seats',                                     10.000, 89.9500,   899.5000, 'VAT19'),
  (3, 'Trial credit — fractional yen rounding edge case',     1.000,  0.0001,     0.0001, 'JCT'),
  (4, 'Precision boundary line — 14,4 max',                   1.000, 99999999.9999, 99999999.9999, 'NONE');

-- payments — fixed-width card_last4 with leading zeros, JSON raw_response
INSERT INTO payment (invoice_id, amount_cents, method, processor_ref, card_last4, raw_response, captured_at) VALUES
  (1, 2499000000, 'wire', 'wire_ref_001', NULL,   '{"bank":"chase","trace":"abc123"}',          TIMESTAMPTZ '2024-01-05 14:22:01+00'),
  (2,    89950,   'card', 'ch_globex_01', '0019', '{"brand":"visa","avs":"Y","cvv":"M"}',       TIMESTAMPTZ '2024-01-10 09:00:00+00'),  -- leading-zero last4
  (2,    89950,   'card', 'ch_globex_02', '0007', '{"brand":"mc","retry":true,"attempt":2}',    TIMESTAMPTZ '2024-01-10 09:01:00+00');

-- warehouses — double precision lat/lng
INSERT INTO warehouse (id, code, name, location_lat, location_lng, tz_name) VALUES
  (1, 'US-WEST', 'Oakland DC',  37.804363, -122.271111, 'America/Los_Angeles'),
  (2, 'EU-CENT', 'Frankfurt DC', 50.110924,    8.682127, 'Europe/Berlin'),
  (3, 'JP-EAST', 'Tokyo DC',     35.689487,  139.691711, 'Asia/Tokyo');
SELECT setval('warehouse_id_seq', 3);

-- products — REAL weight (lossy), numeric array dims, JSON attributes
INSERT INTO product (id, sku, name, description, price, weight_kg, dimensions_cm, attributes) VALUES
  (1, 'SKU-0001', 'Widget Pro',  'A precision widget.', 19.99, 0.125,  ARRAY[10.00, 5.50, 2.25]::numeric(8,2)[], '{"color":"black","material":"alu"}'),
  (2, 'SKU-0002', 'Gadget Max',  NULL,                  149.00, 2.4500001, ARRAY[40.00, 30.00, 12.00]::numeric(8,2)[], '{"color":"white","wireless":true}'),  -- REAL rounding
  (3, 'SKU-0003', 'Тест Юникод', 'Unicode SKU name.',    5.00, NULL,   NULL,                                       '{"unicode":true,"emoji":"🚀"}');         -- nulls + unicode + emoji
SELECT setval('product_id_seq', 3);

INSERT INTO inventory (product_id, warehouse_id, quantity, reorder_level, updated_at) VALUES
  (1, 1, 1200, 100, now()), (1, 2, 300, 50, now()),
  (2, 1, 45, 10, now()),    (3, 3, 0, 5, now());

-- orders — JSON shipping_address, text[] tags, numeric-looking legacy text
INSERT INTO customer_order (id, org_id, user_id, status, placed_at, total, currency, shipping_address, tags, legacy_total_text) VALUES
  (1, 1, 1, 'paid',     TIMESTAMPTZ '2024-03-01 10:00:00+00', 1234.5600, 'USD', '{"line1":"1 Market St","city":"SF","zip":"94105","geo":{"lat":37.79,"lng":-122.40}}', ARRAY['priority','gift'], '1234.5600'),
  (2, 2, 4, 'placed',   TIMESTAMPTZ '2024-03-02 11:30:00+00',  89.9500, 'EUR', '{"line1":"Hauptstr 1","city":"Berlin","zip":"10115"}',                                 ARRAY['eu'],              '89,95'),       -- trap: comma decimal text
  (3, 3, 5, 'cart',     NULL,                                   0.0000, 'JPY', NULL,                                                                                    '{}',                     NULL);
SELECT setval('customer_order_id_seq', 3);

INSERT INTO order_item (order_id, product_id, quantity, unit_price, discount_pct) VALUES
  (1, 1, 2, 19.9900, 0.1000),   -- 10% off
  (1, 2, 1, 149.0000, 0.0000),
  (2, 1, 5, 19.9900, 0.0500);   -- 5% off

-- support tickets — nullable sentiment + resolved_at
INSERT INTO support_ticket (org_id, user_id, priority, subject, body, sentiment, created_at, resolved_at) VALUES
  (1, 2, 'high',   'Cannot log in',          'SSO redirect loops back to login.', -0.45, TIMESTAMPTZ '2024-03-03 08:00:00+00', TIMESTAMPTZ '2024-03-03 09:15:00+00'),
  (2, 4, 'urgent', 'Billing overcharge',     'Charged twice for March.',           NULL, TIMESTAMPTZ '2024-03-04 12:00:00+00', NULL),
  (3, 5, 'low',    'Feature request: 日本語', 'Please add full JP localization.',     0.80, TIMESTAMPTZ '2024-03-05 01:00:00+00', NULL);

-- audit log — inet + nullable actor + JSON payload
INSERT INTO audit_log (actor_user_id, action, entity_type, entity_id, ip_address, payload, occurred_at) VALUES
  (1,    'user.login',     'app_user', 1, '203.0.113.42',        '{"ua":"Mozilla/5.0","mfa":true}',            TIMESTAMPTZ '2024-03-01 08:15:00+00'),
  (2,    'invoice.paid',   'invoice',  1, '198.51.100.7',        '{"amount_cents":2499000000}',                TIMESTAMPTZ '2024-01-05 14:22:01+00'),
  (NULL, 'system.cron',    'job',      0, '::1',                 '{"job":"nightly_rollup","ok":true}',         TIMESTAMPTZ '2024-03-06 03:00:00+00'),  -- IPv6 + null actor
  (4,    'token.created',  'api_token', 1, '2001:db8::8a2e:370:7334', '{"scopes":["read"]}',                  TIMESTAMPTZ '2024-03-02 09:00:00+00'); -- full IPv6

-- event metrics — double precision values
INSERT INTO event_metric (org_id, metric_name, value, dimensions, recorded_at) VALUES
  (1, 'api.latency_ms',  123.456789012345, '{"endpoint":"/v1/users","p":95}', TIMESTAMPTZ '2024-03-06 00:00:00+00'),
  (1, 'mrr.usd',         249900.0,          '{"plan":"enterprise"}',           TIMESTAMPTZ '2024-03-06 00:00:00+00'),
  (2, 'api.error_rate',  0.000123,          '{"endpoint":"/v1/pay"}',          TIMESTAMPTZ '2024-03-06 00:00:00+00');

-- api tokens — fixed-width 64-char hex hash
INSERT INTO api_token (org_id, token_hash, scopes, expires_at, revoked, created_at) VALUES
  (1, '0000000000000000000000000000000000000000000000000000000000000abc', ARRAY['read','write'], TIMESTAMPTZ '2025-01-01 00:00:00+00', false, now()),  -- leading-zero hash
  (2, 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff', ARRAY['read'],         NULL,                                  true,  now());

ANALYZE;
