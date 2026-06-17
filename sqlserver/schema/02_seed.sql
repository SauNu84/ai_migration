-- =============================================================================
-- Phase 1 SQL Server seed (SAU-323) — deterministic, reproducible data that
-- deliberately exercises the hard columns documented in
-- ground_truth/hard_columns.sqlserver.json.
--
-- The values here are the *ground truth*: every edge case (leading-zero
-- fixed-width ids, MONEY/SMALLMONEY 4-dp scale, DATETIME vs DATETIME2 precision,
-- DATETIMEOFFSET tz offsets, UNIQUEIDENTIFIER GUIDs, NVARCHAR unicode, high-scale
-- DECIMAL rates, numeric-looking text, JSON-in-NVARCHAR, IPv4/IPv6, nulls) is
-- chosen so the core verification loop can prove byte/precision-exact round-trips,
-- not just "it loaded".
--
-- ROWVERSION columns (company.row_version, audit_trail.row_version) are
-- auto-generated and intentionally NOT inserted.
-- =============================================================================

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

-- companies — UNIQUEIDENTIFIER PK + DATETIMEOFFSET offsets (+00,+02,+09) + JSON + nulls
INSERT INTO dbo.company (id, name, domain, is_active, metadata_json, created_at) VALUES
  ('11111111-1111-1111-1111-111111111111', N'Acme Mutual',     'acme.example',   1, N'{"tier":"enterprise","lines":["auto","home"],"states":250}',      '2023-01-15 09:30:00 +00:00'),
  ('22222222-2222-2222-2222-222222222222', N'Globex Re GmbH',  'globex.example', 1, N'{"tier":"growth","region":"eu","vat":"DE123456789"}',             '2023-06-01 23:59:59 +02:00'),  -- tz offset edge
  ('33333333-3333-3333-3333-333333333333', N'Sōko Insurance',  'soko.example',   1, N'{"tier":"growth","locale":"ja-JP"}',                              '2024-02-29 12:00:00 +09:00'),  -- unicode + leap day + JST
  ('44444444-4444-4444-4444-444444444444', N'Dormant Holdings', NULL,            0, NULL,                                                               '2022-11-30 00:00:00 +00:00'); -- null domain + null metadata
GO

-- gl_accounts — fixed-width leading-zero account_code + CHECK-domain account_type
INSERT INTO dbo.gl_account (company_id, account_code, name, account_type, is_active) VALUES
  ('11111111-1111-1111-1111-111111111111', '00001000', N'Cash',                'asset',     1),
  ('11111111-1111-1111-1111-111111111111', '00002000', N'Premiums Receivable', 'asset',     1),
  ('11111111-1111-1111-1111-111111111111', '00004000', N'Earned Premium',      'revenue',   1),
  ('11111111-1111-1111-1111-111111111111', '00005000', N'Claims Expense',      'expense',   1),
  ('11111111-1111-1111-1111-111111111111', '00003000', N'Loss Reserves',       'liability', 1),
  ('22222222-2222-2222-2222-222222222222', '00001000', N'Bargeld',             'asset',     1),
  ('22222222-2222-2222-2222-222222222222', '00004000', N'Verdiente Prämie',    'revenue',   1),
  ('33333333-3333-3333-3333-333333333333', '00001000', N'現金',                 'asset',     1);
GO

-- fiscal_periods
INSERT INTO dbo.fiscal_period (company_id, period_code, starts_on, ends_on, status) VALUES
  ('11111111-1111-1111-1111-111111111111', '202401', '2024-01-01', '2024-01-31', 'closed'),
  ('11111111-1111-1111-1111-111111111111', '202402', '2024-02-01', '2024-02-29', 'closed'),  -- leap month
  ('11111111-1111-1111-1111-111111111111', '202403', '2024-03-01', '2024-03-31', 'open'),
  ('22222222-2222-2222-2222-222222222222', '202401', '2024-01-01', '2024-01-31', 'locked'),
  ('33333333-3333-3333-3333-333333333333', '202402', '2024-02-01', '2024-02-29', 'open');
GO

-- journal_entries — DATETIME (legacy ~3.33ms) vs DATETIME2(7) (100-ns) vs DATETIMEOFFSET
INSERT INTO dbo.journal_entry (id, company_id, period_id, memo, source_ref, entered_at, posted_at, created_offset) VALUES
  ('a0000001-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111', 1, N'Premium booking',  'JE-2024-0001',
     '2024-01-05 08:15:00.123',     '2024-01-05 08:15:00.1234567', '2024-01-05 08:15:00.1234567 +00:00'),  -- DATETIME rounds .123->.123333; DATETIME2 keeps 7dp
  ('a0000002-0000-0000-0000-000000000002', '11111111-1111-1111-1111-111111111111', 2, N'Claims accrual',   'JE-2024-0002',
     '2024-02-29 23:59:59.997',     '2024-02-29 23:59:59.9999999', '2024-02-29 23:59:59.9999999 +00:00'),  -- DATETIME .997 boundary; DATETIME2 max sub-second
  ('a0000003-0000-0000-0000-000000000003', '22222222-2222-2222-2222-222222222222', 4, N'EU posting',       'JE-2024-0003',
     '2024-01-10 09:00:00.000',     '2024-01-10 09:00:00.0000000', '2024-01-10 11:00:00.0000000 +02:00');  -- same instant, +02 offset
GO

-- journal_lines — MONEY debit/credit at scale-4 (4th dp must survive)
INSERT INTO dbo.journal_line (entry_id, account_id, debit, credit, currency, memo) VALUES
  ('a0000001-0000-0000-0000-000000000001', 2, 24990.0000,     0.0000, 'USD', N'Premiums receivable'),
  ('a0000001-0000-0000-0000-000000000001', 3,     0.0000, 24990.0000, 'USD', N'Earned premium'),
  ('a0000002-0000-0000-0000-000000000002', 4, 15000.3300,     0.0000, 'USD', N'Claims expense'),
  ('a0000002-0000-0000-0000-000000000002', 5,     0.0000, 15000.3300, 'USD', N'Loss reserve'),
  ('a0000001-0000-0000-0000-000000000001', 1,     0.0001,     0.0000, 'USD', N'Sub-cent rounding plug — 4th dp survives'),  -- MONEY 4-dp trap
  ('a0000003-0000-0000-0000-000000000003', 6,   899.5000,     0.0000, 'EUR', N'EU cash');
GO

-- policyholders — unicode names, leading-zero SSN, email length, nulls
INSERT INTO dbo.policyholder (company_id, full_name, email, phone, ssn_last4, date_of_birth, preferences_json, created_at) VALUES
  ('11111111-1111-1111-1111-111111111111', N'Alice Owner',  'alice@acme.example',  '+1-415-555-0100', '0042', '1980-05-01', N'{"channel":"email"}',           '2023-01-15 09:31:00 +00:00'),
  ('11111111-1111-1111-1111-111111111111', N'Bob Holder',   'bob@acme.example',    '+1 (415) 555-0101', '0007', '1975-11-20', N'{}',                          '2023-01-16 10:00:00 +00:00'),
  ('11111111-1111-1111-1111-111111111111', NULL,            'noname@acme.example', NULL,                NULL,   NULL,         NULL,                            '2023-02-01 00:00:00 +00:00'),  -- many nulls
  ('22222222-2222-2222-2222-222222222222', N'Carl Müller',  'carl@globex.example', '+49 30 1234567',  '1000', '1968-02-29', N'{"locale":"de-DE"}',           '2023-06-01 22:00:00 +00:00'),  -- unicode name + leap DOB
  ('33333333-3333-3333-3333-333333333333', N'田中 太郎',     'tanaka@soko.example', '+81-3-1234-5678', '0815', '1990-07-07', N'{"locale":"ja-JP"}',           '2024-02-29 03:00:00 +00:00'); -- CJK name
GO

-- agents — high-scale commission rate
INSERT INTO dbo.agent (company_id, name, license_no, commission_rate, hired_on) VALUES
  ('11111111-1111-1111-1111-111111111111', N'Dana Broker',  'LIC-US-0001', 0.083325, '2019-04-01'),  -- 6-dp rate
  ('22222222-2222-2222-2222-222222222222', N'Erik Vermittler', 'LIC-EU-0007', 0.150000, '2020-09-15'),
  ('33333333-3333-3333-3333-333333333333', N'佐藤 代理',     'LIC-JP-0042', 0.100000, '2021-01-10');
GO

-- product_catalog — REAL rate factor (lossy float4) + JSON attrs with unicode/emoji
INSERT INTO dbo.product_catalog (code, name, description, base_rate_factor, attributes_json) VALUES
  ('AUTO-STD',  N'Auto Standard',  N'Standard auto policy.', 1.2500000, N'{"peril":"collision","territory":"urban"}'),
  ('HOME-PLUS', N'Home Plus',      NULL,                     2.4500001, N'{"peril":"fire","wind":true}'),               -- REAL rounding edge
  ('LIFE-ユニ',  N'Life Unicode',   N'Unicode product name.',  NULL,      N'{"unicode":true,"emoji":"🚀","note":"テスト"}'); -- null + unicode + emoji
GO

-- policies — MONEY premium edges + SMALLMONEY fee + DATETIMEOFFSET + numeric-text
INSERT INTO dbo.policy (id, company_id, policyholder_id, agent_id, product_id, policy_number, status, premium, policy_fee, effective_at, expires_on, legacy_premium_text) VALUES
  ('b0000001-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111', 1, 1, 1, 'POL-00000001', 'active',   2499.5000,  25.0000, '2024-01-01 00:00:00 +00:00', '2025-01-01', '2499.5000'),
  ('b0000002-0000-0000-0000-000000000002', '22222222-2222-2222-2222-222222222222', 4, 2, 2, 'POL-00000002', 'active',    899.5000,   1.9900, '2024-01-01 00:00:00 +02:00', '2025-01-01', '899,50'),       -- comma-decimal text trap
  ('b0000003-0000-0000-0000-000000000003', '33333333-3333-3333-3333-333333333333', 5, 3, 3, 'POL-00000003', 'bound',  999999.9999,   0.0001, '2024-02-29 12:00:00 +09:00', NULL,         '999999.9999'),  -- MONEY hi-precision + SMALLMONEY 4-dp boundary
  ('b0000004-0000-0000-0000-000000000004', '11111111-1111-1111-1111-111111111111', 2, 1, 1, 'POL-00000004', 'lapsed',      0.0001,   0.0000, '2024-03-01 00:00:00 +00:00', NULL,         '0.0001');       -- sub-cent premium, 4th dp must survive
GO

-- coverage — DECIMAL(18,2) limits + MONEY deductible + SMALLMONEY portion
INSERT INTO dbo.coverage (policy_id, coverage_type, limit_amount, deductible, premium_portion) VALUES
  ('b0000001-0000-0000-0000-000000000001', N'collision',  50000.00,   500.0000,  1200.0000),
  ('b0000001-0000-0000-0000-000000000001', N'liability', 300000.00,     0.0000,  1299.5000),
  ('b0000002-0000-0000-0000-000000000002', N'fire',      250000.00,  1000.0000,   899.5000),
  ('b0000003-0000-0000-0000-000000000003', N'life',    10000000.00,     0.0000,    -1.0000);   -- negative SMALLMONEY edge
GO

-- claims — MONEY amount + FLOAT reserve (currency-as-FLOAT trap) + DATETIME2 + DTO + JSON
INSERT INTO dbo.claim (id, company_id, policy_id, claim_number, status, claim_amount, reserve_amount, reported_at, incident_at, metadata_json, description) VALUES
  ('c0000001-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111', 'b0000001-0000-0000-0000-000000000001', 'CLM-0000000001', 'approved', 12500.7500, 15000.33,         '2024-03-03 08:00:00.1234567', '2024-03-02 19:30:00 -05:00', N'{"cause":"collision","fault":"third_party","photos":3}', N'Rear-end collision on I-880.'),
  ('c0000002-0000-0000-0000-000000000002', '22222222-2222-2222-2222-222222222222', 'b0000002-0000-0000-0000-000000000002', 'CLM-0000000002', 'open',       899.9500,   1000.0,           '2024-03-04 12:00:00.0000001', '2024-03-04 10:00:00 +02:00', N'{"cause":"fire","emoji":"🔥","note":"Küche"}',          N'Kitchen fire — partial loss.'),
  ('c0000003-0000-0000-0000-000000000003', '33333333-3333-3333-3333-333333333333', 'b0000003-0000-0000-0000-000000000003', 'CLM-0000000003', 'review',  500000.0000,  499999.9999999, '2024-02-29 23:59:59.9999999', NULL,                          N'{"cause":"life","locale":"ja-JP"}',                     NULL);  -- FLOAT reserve cannot hold 4-dp currency exactly
GO

-- claim_payments — SMALLMONEY fee at scale-4
INSERT INTO dbo.claim_payment (claim_id, amount, fee, method, processor_ref, paid_at) VALUES
  ('c0000001-0000-0000-0000-000000000001', 12000.0000,  1.9900, 'ach',   'ach_ref_001', '2024-03-10 14:00:00.0000000'),
  ('c0000001-0000-0000-0000-000000000001',   500.7500,  0.0001, 'check', 'chk_000123',  '2024-03-11 09:30:00.5000000'),  -- SMALLMONEY 4-dp fee
  ('c0000002-0000-0000-0000-000000000002',   899.9500, 12.5000, 'wire',  'wire_eu_77',  '2024-03-12 08:00:00.0000000');
GO

-- payment_methods — leading-zero fixed-width last4
INSERT INTO dbo.payment_method (policyholder_id, kind, card_last4, routing_last4, bank_name, created_at) VALUES
  (1, 'card', '0019', NULL,   N'Chase',        '2024-01-02 10:00:00 +00:00'),  -- leading-zero card last4
  (2, 'ach',  NULL,   '0007', N'Bank of West', '2024-01-03 10:00:00 +00:00'),
  (4, 'wire', NULL,   NULL,   N'Sparkasse',    '2024-01-04 10:00:00 +02:00');
GO

-- transaction_ledger — DECIMAL(18,8) FX rate at 8-dp + MONEY amount
INSERT INTO dbo.transaction_ledger (company_id, account_id, amount, currency, exchange_rate, fx_amount, kind, occurred_at) VALUES
  ('11111111-1111-1111-1111-111111111111', 1, 24990.0000, 'USD', 1.00000000,   24990.0000, 'posting', '2024-01-05 08:15:00 +00:00'),
  ('22222222-2222-2222-2222-222222222222', 6,   899.5000, 'EUR', 1.08734500,     978.0470, 'posting', '2024-01-10 09:00:00 +02:00'),  -- 8-dp rate
  ('33333333-3333-3333-3333-333333333333', 8, 12000.0000, 'JPY', 0.00667125,      80.0550, 'posting', '2024-02-29 12:00:00 +09:00');  -- tiny 8-dp rate
GO

-- exchange_rates — high-scale DECIMAL
INSERT INTO dbo.exchange_rate (base_ccy, quote_ccy, rate, as_of) VALUES
  ('EUR', 'USD', 1.08734500, '2024-01-10'),
  ('JPY', 'USD', 0.00667125, '2024-02-29'),
  ('GBP', 'USD', 1.27500000, '2024-03-01');
GO

-- documents — fixed-width CHAR(64) hash (leading zeros) + case-sensitive collation title
INSERT INTO dbo.document (company_id, claim_id, file_name, file_hash, mime_type, title_cs, uploaded_at) VALUES
  ('11111111-1111-1111-1111-111111111111', 'c0000001-0000-0000-0000-000000000001', N'estimate.pdf',
     '0000000000000000000000000000000000000000000000000000000000000abc', 'application/pdf', N'Estimate', '2024-03-05 10:00:00.0000000'),  -- leading-zero hash
  ('11111111-1111-1111-1111-111111111111', 'c0000001-0000-0000-0000-000000000001', N'photo.jpg',
     'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff', 'image/jpeg',      N'ESTIMATE', '2024-03-05 10:01:00.0000000'),  -- case differs from 'Estimate' (CS collation)
  ('22222222-2222-2222-2222-222222222222', NULL, N'police_report.pdf',
     '00000000000000000000000000000000000000000000000000000000deadbeef', 'application/pdf', N'Bericht',  '2024-03-06 12:00:00.0000000');
GO

-- audit_trail — IPv4 + IPv6 + null actor + JSON (rowversion auto)
INSERT INTO dbo.audit_trail (id, actor_user, action, entity_type, entity_id, ip_address, payload_json, occurred_at) VALUES
  ('d0000001-0000-0000-0000-000000000001', N'alice@acme.example', 'policy.bind',    'policy', 'b0000001-0000-0000-0000-000000000001', '203.0.113.42',             N'{"ua":"Mozilla/5.0","mfa":true}',   '2024-01-01 00:00:00.1234567'),
  ('d0000002-0000-0000-0000-000000000002', N'bob@acme.example',   'claim.approve',  'claim',  'c0000001-0000-0000-0000-000000000001', '198.51.100.7',             N'{"amount":12500.75}',               '2024-03-10 13:59:00.0000000'),
  ('d0000003-0000-0000-0000-000000000003', NULL,                  'system.rollup',  'job',    '0',                                    '::1',                       N'{"job":"nightly","ok":true}',       '2024-03-06 03:00:00.0000000'),  -- IPv6 loopback + null actor
  ('d0000004-0000-0000-0000-000000000004', N'佐藤 代理',           'document.upload','document','3',                                   '2001:db8::8a2e:370:7334',   N'{"scopes":["read"]}',               '2024-03-06 12:00:00.0000000'); -- full IPv6
GO
