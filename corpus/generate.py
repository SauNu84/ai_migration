#!/usr/bin/env python3
"""SAU-532 — Phase-1 corpus generator: the volume bar's ungated half.

[SAU-480](SAU-480) set the Phase-1 volume bar as a **shape**, not a total, and split it
into two tiers. This module builds the corpus both tiers load, and is what lets the
proxy tier run today without the Snowflake credential the gate tier waits on:

| tier  | rows  | target                | what it is for                                  |
|-------|-------|-----------------------|-------------------------------------------------|
| proxy | 1M    | DuckDB (proxy)        | build + validate the generator and the loop NOW |
| gate  | 10M   | live Snowflake        | closes SAU-310; still blocked on the credential |

**Shape spec** (the total alone is under-specified — SAU-480):

* ≥ 25 entities (Phase 0 has 16/17);
* ≥ 50 % of all rows in ONE entity (5M of 10M at gate tier, scaled at proxy tier);
* ≥ 3 entities with **no unique `id`** — see "the defect this is built to catch";
* byte-identical across runs from a fixed seed — `3/3 consecutive clean runs` requires
  this by construction, and a corpus that drifts between runs cannot tell a flaky loop
  from a flaky source.

**Synthesized, never obtained.** CI cannot hold customer rows, and real data *dilutes*
trap density rather than raising it: a million rows of production noise contain fewer
deliberate edge cases per row than a hundred hand-authored ones. So volume is generated
in-database from `generate_series` with seed-derived mixing constants (the same method
as `profile/stage_profile.py`'s probe, generalized), and the traps are hand-authored
literals in their own small tables — orthogonal to the volume, because volume will not
find them and only deliberate values will.

**Determinism, and what "byte-identical" means here.** Every value is a pure function
of `(seed, row ordinal)`: no `random()`, no `now()`, no sequence whose value depends on
load order. The generator emits SQL text whose SHA-256 is the corpus identity at rest;
`--apply` then loads it and records a per-entity **content digest computed in-database
over rows sorted by every column** — so it is the same number regardless of physical
order, which is precisely the property the reconciliation checksum does *not* have (see
below). Two runs are the same corpus iff both digests match.

## The defect this corpus is built to catch

`core/reconcile.py::_gold_sql` orders by `"id"` **only when the entity has one**, and
`ChecksumAccumulator` folds the row *ordinal* into the digest, so the checksum is
order-sensitive. An entity without a unique `id` therefore gets whatever scan order the
engine happens to produce on each side — deterministic at 58 rows, not at 500k with
parallel row-group scans — and the source walk and the Gold walk can disagree while
every value is intact: **a flaky dirty verdict indistinguishable from real corruption.**
Four of the entities below have no `id` on purpose, one of them additionally carries
exact-duplicate rows (which defeats "sort by everything" as a cheap fix — the *multiset*
has repeats). If a run surfaces this, the bar is not lowered to make it green: that is
the bug the bar was built to find.

(Since SAU-548 the loop gives keyless entities an order-independent multiset digest, so
the paragraph above describes what this corpus *guards against regressing*, not a live
defect.)

## The seven trap types Phase 0 never seeded

`BYTEA`, `UUID`, `INTERVAL`, `MONEY`, `XML`, `TIME`, `SMALLINT`. Each is planted as a
hand-authored literal in a VALUES-only INSERT so bullet 0 can audit that it landed (an
`INSERT … SELECT` states no literal claim, so the volume tables are deliberately outside
bullet 0's reach). What the loop *does* with those types is the loop's finding to make,
not this generator's to pre-empt.

Usage:
    python3 corpus/generate.py --rows 1000000 --seed 20260921 --out artifacts/corpus
    PGHOST=/tmp PGPORT=5599 PGUSER=postgres PGDATABASE=phase0 \\
        python3 corpus/generate.py --rows 1000000 --apply     # load + in-DB digests
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

DEFAULT_SEED = 20260921
DEFAULT_SCHEMA = "phase1"

# ── seed → mixing constants ──────────────────────────────────────────────────
#
# splitmix64 is the whole PRNG: it is only ever used to derive a handful of constants
# from the seed, never per row. Per-row variety comes from modular arithmetic on the
# row ordinal inside the database, which is deterministic by construction and needs no
# client-side state. Two multipliers (A1 odd, A2 odd) and two offsets give every column
# a different but reproducible permutation of the ordinal.

_MASK = (1 << 64) - 1
_M31 = 2_147_483_647          # 2^31 - 1, prime — the modulus for the mixing hash


def _splitmix64(state: int) -> tuple[int, int]:
    state = (state + 0x9E3779B97F4A7C15) & _MASK
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
    return state, z ^ (z >> 31)


@dataclass(frozen=True)
class Mix:
    """Seed-derived constants that parameterize every in-database expression."""
    seed: int
    a1: int   # odd multiplier, < 2^20 so g*a1 stays far inside BIGINT at 10M rows
    a2: int
    b1: int
    b2: int

    @classmethod
    def from_seed(cls, seed: int) -> "Mix":
        st, x1 = _splitmix64(seed)
        st, x2 = _splitmix64(st)
        st, x3 = _splitmix64(st)
        st, x4 = _splitmix64(st)
        return cls(seed=seed,
                   a1=(x1 % (1 << 20)) | 1, a2=(x2 % (1 << 20)) | 1,
                   b1=x3 % _M31, b2=x4 % _M31)

    def h1(self, g: str = "g") -> str:
        """A seed-keyed permutation-ish of the ordinal, as SQL. Always non-negative."""
        return f"(({g} * {self.a1} + {self.b1}) % {_M31})"

    def h2(self, g: str = "g") -> str:
        return f"(({g} * {self.a2} + {self.b2}) % {_M31})"


# ── entity catalogue ─────────────────────────────────────────────────────────
#
# `share` is the fraction of the tier's total rows; `min_rows` floors small dimensions
# so the shape survives a tiny `--rows` in tests. The dominant entity is `event`, which
# absorbs the remainder so the total is *exactly* `--rows`. `has_id` is the shape-spec
# flag the tests assert on; it is also what decides whether the loop's ORDER BY exists.

@dataclass(frozen=True)
class Entity:
    name: str
    share: float
    has_id: bool
    ddl: str
    fill: str                        # INSERT … SELECT … FROM generate_series(1, {n}) g
    min_rows: int = 1
    depends: tuple[str, ...] = ()
    note: str = ""


def _volume_entities(mx: Mix, schema: str) -> list[Entity]:
    h1, h2 = mx.h1(), mx.h2()
    S = schema
    E: list[Entity] = []

    # ── dimensions (with id) ────────────────────────────────────────────────
    E.append(Entity("org", 0.001, True, min_rows=20, ddl=f"""
CREATE TABLE {S}.org (
  id          BIGINT PRIMARY KEY,
  name        VARCHAR(255) NOT NULL,
  domain      VARCHAR(255),
  is_active   BOOLEAN NOT NULL,
  metadata    JSONB,
  created_at  TIMESTAMPTZ NOT NULL
);""", fill=f"""
INSERT INTO {S}.org
SELECT g, 'Org ' || g, 'org' || g || '.example.com', ({h1} % 10) <> 0,
       CASE WHEN g % 3 = 0 THEN NULL ELSE jsonb_build_object('tier', ({h2} % 4), 'seed', {mx.seed}) END,
       timestamptz '2023-01-01 00:00:00+00' + (({h1} % 500000) || ' seconds')::interval
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("plan", 0.00001, True, min_rows=8, ddl=f"""
CREATE TABLE {S}.plan (
  id            BIGINT PRIMARY KEY,
  code          VARCHAR(40) NOT NULL,
  name          VARCHAR(120) NOT NULL,
  monthly_price NUMERIC(12,2) NOT NULL,
  currency      {S}.currency_code NOT NULL,
  features      TEXT[] NOT NULL
);""", fill=f"""
INSERT INTO {S}.plan
SELECT g, 'PLAN-' || lpad(g::text, 3, '0'), 'Plan ' || g,
       (({h1} % 100000)::numeric / 100),
       (ARRAY['USD','EUR','GBP','JPY','VND']::{S}.currency_code[])[(g % 5) + 1],
       ARRAY['sso', 'audit', 'f' || ({h2} % 50)]
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("app_user", 0.01, True, min_rows=50, depends=("org",), ddl=f"""
CREATE TABLE {S}.app_user (
  id            BIGINT PRIMARY KEY,
  org_id        BIGINT NOT NULL REFERENCES {S}.org(id),
  email         VARCHAR(320) NOT NULL,
  full_name     VARCHAR(200),
  role          {S}.user_role NOT NULL,
  phone         VARCHAR(32),
  ssn_last4     CHAR(4),
  preferences   JSONB,
  last_login_at TIMESTAMP,
  created_at    TIMESTAMPTZ NOT NULL
);""", fill=f"""
INSERT INTO {S}.app_user
SELECT g, ({h1} % {{org}}) + 1,
       'user' || g || '@example.com',
       CASE WHEN g % 7 = 0 THEN NULL ELSE 'Person Number ' || g END,
       (ARRAY['owner','admin','member','billing','readonly']::{S}.user_role[])[(g % 5) + 1],
       CASE WHEN g % 5 = 0 THEN NULL ELSE '+1-555-' || lpad(({h2} % 10000)::text, 4, '0') END,
       lpad(({h1} % 10000)::text, 4, '0'),
       jsonb_build_object('theme', CASE WHEN g % 2 = 0 THEN 'dark' ELSE 'light' END,
                          'digest', (g % 3 = 0), 'rank', {h2}),
       CASE WHEN g % 11 = 0 THEN NULL
            ELSE timestamp '2024-01-01 00:00:00' + (({h1} % 31536000) || ' seconds')::interval END,
       timestamptz '2024-01-01 00:00:00+00' + (g || ' seconds')::interval
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("subscription", 0.01, True, min_rows=20, depends=("org", "plan"), ddl=f"""
CREATE TABLE {S}.subscription (
  id            BIGINT PRIMARY KEY,
  org_id        BIGINT NOT NULL REFERENCES {S}.org(id),
  plan_id       BIGINT NOT NULL REFERENCES {S}.plan(id),
  status        {S}.subscription_status NOT NULL,
  seats         INTEGER NOT NULL,
  mrr_cents     BIGINT NOT NULL,
  started_on    DATE NOT NULL,
  renews_at     TIMESTAMPTZ,
  trial_ends_at TIMESTAMPTZ
);""", fill=f"""
INSERT INTO {S}.subscription
SELECT g, ({h1} % {{org}}) + 1, ({h2} % {{plan}}) + 1,
       (ARRAY['trialing','active','past_due','canceled','paused']::{S}.subscription_status[])[(g % 5) + 1],
       ({h1} % 500) + 1, ({h2} % 10000000),
       date '2023-01-01' + ({h1} % 700)::int,
       CASE WHEN g % 4 = 0 THEN NULL ELSE timestamptz '2025-01-01 00:00:00+00' + (({h2} % 31536000) || ' seconds')::interval END,
       CASE WHEN g % 3 = 0 THEN timestamptz '2024-06-01 00:00:00+02' + (g || ' minutes')::interval ELSE NULL END
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("invoice", 0.02, True, min_rows=40, depends=("org", "subscription"), ddl=f"""
CREATE TABLE {S}.invoice (
  id              BIGINT PRIMARY KEY,
  org_id          BIGINT NOT NULL REFERENCES {S}.org(id),
  subscription_id BIGINT REFERENCES {S}.subscription(id),
  number          VARCHAR(40) NOT NULL,
  total_amount    NUMERIC(14,4) NOT NULL,
  tax_rate        NUMERIC(9,6) NOT NULL,
  currency        {S}.currency_code NOT NULL,
  status          VARCHAR(20) NOT NULL,
  issued_at       TIMESTAMPTZ NOT NULL,
  due_date        DATE,
  paid_at         TIMESTAMPTZ
);""", fill=f"""
INSERT INTO {S}.invoice
SELECT g, ({h1} % {{org}}) + 1,
       CASE WHEN g % 9 = 0 THEN NULL ELSE ({h2} % {{subscription}}) + 1 END,
       'INV-' || lpad(g::text, 9, '0'),
       (({h1} % 100000000)::numeric / 10000),
       (({h2} % 300000)::numeric / 1000000),
       (ARRAY['USD','EUR','GBP','JPY','VND']::{S}.currency_code[])[(g % 5) + 1],
       (ARRAY['open','paid','void','uncollectible'])[(g % 4) + 1],
       timestamptz '2024-01-01 00:00:00+00' + (({h1} % 31536000) || ' seconds')::interval,
       CASE WHEN g % 6 = 0 THEN NULL ELSE date '2024-01-01' + ({h2} % 400)::int END,
       CASE WHEN g % 2 = 0 THEN timestamptz '2024-01-01 00:00:00+09' + (({h2} % 31536000) || ' seconds')::interval ELSE NULL END
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("invoice_line", 0.04, True, min_rows=80, depends=("invoice",), ddl=f"""
CREATE TABLE {S}.invoice_line (
  id          BIGINT PRIMARY KEY,
  invoice_id  BIGINT NOT NULL REFERENCES {S}.invoice(id),
  description VARCHAR(1000) NOT NULL,
  quantity    NUMERIC(12,3) NOT NULL,
  unit_price  NUMERIC(14,4) NOT NULL,
  amount      NUMERIC(14,4) NOT NULL,
  tax_code    VARCHAR(20)
);""", fill=f"""
INSERT INTO {S}.invoice_line
SELECT g, ({h1} % {{invoice}}) + 1, 'Line item ' || g || ' — ' || repeat('x', ({h2} % 40)::int),
       (({h1} % 100000)::numeric / 1000), (({h2} % 10000000)::numeric / 10000),
       ((({h1} % 100000)::numeric / 1000) * (({h2} % 10000000)::numeric / 10000)),
       CASE WHEN g % 3 = 0 THEN NULL ELSE 'TX' || ({h1} % 20) END
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("payment", 0.02, True, min_rows=40, depends=("invoice",), ddl=f"""
CREATE TABLE {S}.payment (
  id            BIGINT PRIMARY KEY,
  invoice_id    BIGINT NOT NULL REFERENCES {S}.invoice(id),
  amount_cents  BIGINT NOT NULL,
  method        {S}.payment_method NOT NULL,
  processor_ref VARCHAR(80),
  card_last4    CHAR(4),
  raw_response  JSONB,
  captured_at   TIMESTAMPTZ NOT NULL
);""", fill=f"""
INSERT INTO {S}.payment
SELECT g, ({h1} % {{invoice}}) + 1, ({h2} % 100000000),
       (ARRAY['card','ach','wire','paypal','credit']::{S}.payment_method[])[(g % 5) + 1],
       CASE WHEN g % 8 = 0 THEN NULL ELSE 'ch_' || lpad(({h1} % 100000000)::text, 8, '0') END,
       lpad(({h2} % 10000)::text, 4, '0'),
       CASE WHEN g % 4 = 0 THEN NULL ELSE jsonb_build_object('ok', g % 2 = 0, 'code', ({h1} % 1000)) END,
       timestamptz '2024-01-01 00:00:00+00' + (({h2} % 31536000) || ' seconds')::interval
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("product", 0.002, True, min_rows=20, ddl=f"""
CREATE TABLE {S}.product (
  id            BIGINT PRIMARY KEY,
  sku           VARCHAR(64) NOT NULL,
  name          VARCHAR(255) NOT NULL,
  attributes    JSONB,
  dimensions_cm NUMERIC(8,2)[],
  weight_kg     REAL,
  is_active     BOOLEAN NOT NULL
);""", fill=f"""
INSERT INTO {S}.product
SELECT g, 'SKU-' || lpad(g::text, 8, '0'), 'Product ' || g,
       CASE WHEN g % 5 = 0 THEN NULL ELSE jsonb_build_object('color', (ARRAY['red','green','blue'])[(g % 3) + 1], 'w', ({h1} % 100)) END,
       ARRAY[(({h1} % 10000)::numeric / 100), (({h2} % 10000)::numeric / 100)]::numeric(8,2)[],
       (({h1} % 100000)::real / 1000),
       g % 13 <> 0
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("customer_order", 0.02, True, min_rows=40, depends=("org", "app_user"), ddl=f"""
CREATE TABLE {S}.customer_order (
  id                BIGINT PRIMARY KEY,
  org_id            BIGINT NOT NULL REFERENCES {S}.org(id),
  placed_by         BIGINT REFERENCES {S}.app_user(id),
  status            {S}.order_status NOT NULL,
  legacy_total_text VARCHAR(32),
  shipping_address  TEXT,
  tags              TEXT[],
  placed_at         TIMESTAMPTZ NOT NULL
);""", fill=f"""
INSERT INTO {S}.customer_order
SELECT g, ({h1} % {{org}}) + 1,
       CASE WHEN g % 10 = 0 THEN NULL ELSE ({h2} % {{app_user}}) + 1 END,
       (ARRAY['cart','placed','paid','fulfilled','refunded','canceled']::{S}.order_status[])[(g % 6) + 1],
       CASE WHEN g % 3 = 0 THEN '00' || ({h1} % 1000) ELSE ({h2} % 100000)::text || ',' || lpad(({h1} % 100)::text, 2, '0') END,
       CASE WHEN g % 4 = 0 THEN NULL ELSE ({h1} % 9999) || ' Main St\\nSuite ' || ({h2} % 500) END,
       CASE WHEN g % 5 = 0 THEN NULL ELSE ARRAY['t' || ({h1} % 7), 'p' || ({h2} % 3)] END,
       timestamptz '2024-01-01 00:00:00+00' + (({h1} % 31536000) || ' seconds')::interval
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("order_item", 0.04, True, min_rows=80, depends=("customer_order", "product"), ddl=f"""
CREATE TABLE {S}.order_item (
  id           BIGINT PRIMARY KEY,
  order_id     BIGINT NOT NULL REFERENCES {S}.customer_order(id),
  product_id   BIGINT NOT NULL REFERENCES {S}.product(id),
  quantity     INTEGER NOT NULL,
  unit_price   NUMERIC(14,4) NOT NULL,
  discount_pct DOUBLE PRECISION
);""", fill=f"""
INSERT INTO {S}.order_item
SELECT g, ({h1} % {{customer_order}}) + 1, ({h2} % {{product}}) + 1, ({h1} % 20) + 1,
       (({h2} % 10000000)::numeric / 10000),
       CASE WHEN g % 3 = 0 THEN NULL ELSE ({h1} % 10000)::double precision / 100000 END
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("shipment", 0.01, True, min_rows=20, depends=("customer_order",), ddl=f"""
CREATE TABLE {S}.shipment (
  id           BIGINT PRIMARY KEY,
  order_id     BIGINT NOT NULL REFERENCES {S}.customer_order(id),
  carrier      VARCHAR(40) NOT NULL,
  tracking_no  VARCHAR(64),
  shipped_at   TIMESTAMPTZ,
  delivered_on DATE,
  weight_kg    REAL
);""", fill=f"""
INSERT INTO {S}.shipment
SELECT g, ({h1} % {{customer_order}}) + 1, (ARRAY['ups','fedex','dhl','usps'])[(g % 4) + 1],
       CASE WHEN g % 6 = 0 THEN NULL ELSE '1Z' || lpad(({h2} % 100000000)::text, 8, '0') END,
       CASE WHEN g % 5 = 0 THEN NULL ELSE timestamptz '2024-01-01 00:00:00-05' + (({h1} % 31536000) || ' seconds')::interval END,
       CASE WHEN g % 3 = 0 THEN NULL ELSE date '2024-01-01' + ({h2} % 400)::int END,
       (({h1} % 100000)::real / 1000)
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("support_ticket", 0.005, True, min_rows=20, depends=("org", "app_user"), ddl=f"""
CREATE TABLE {S}.support_ticket (
  id          BIGINT PRIMARY KEY,
  org_id      BIGINT NOT NULL REFERENCES {S}.org(id),
  opened_by   BIGINT REFERENCES {S}.app_user(id),
  priority    {S}.ticket_priority NOT NULL,
  subject     VARCHAR(500) NOT NULL,
  opened_at   TIMESTAMPTZ NOT NULL,
  closed_at   TIMESTAMPTZ
);""", fill=f"""
INSERT INTO {S}.support_ticket
SELECT g, ({h1} % {{org}}) + 1, CASE WHEN g % 7 = 0 THEN NULL ELSE ({h2} % {{app_user}}) + 1 END,
       (ARRAY['low','normal','high','urgent']::{S}.ticket_priority[])[(g % 4) + 1],
       'Ticket ' || g || ': ' || repeat('lorem ', ({h1} % 10)::int),
       timestamptz '2024-01-01 00:00:00+00' + (({h1} % 31536000) || ' seconds')::interval,
       CASE WHEN g % 2 = 0 THEN timestamptz '2024-01-01 00:00:00+00' + (({h1} % 31536000) + ({h2} % 100000) || ' seconds')::interval ELSE NULL END
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("ticket_message", 0.02, True, min_rows=40, depends=("support_ticket",), ddl=f"""
CREATE TABLE {S}.ticket_message (
  id         BIGINT PRIMARY KEY,
  ticket_id  BIGINT NOT NULL REFERENCES {S}.support_ticket(id),
  author     VARCHAR(120),
  body       TEXT NOT NULL,
  is_internal BOOLEAN NOT NULL,
  sent_at    TIMESTAMPTZ NOT NULL
);""", fill=f"""
INSERT INTO {S}.ticket_message
SELECT g, ({h1} % {{support_ticket}}) + 1, CASE WHEN g % 4 = 0 THEN NULL ELSE 'agent' || ({h2} % 50) END,
       'Message body ' || g || ' ' || repeat('. ', ({h1} % 30)::int), g % 5 = 0,
       timestamptz '2024-01-01 00:00:00+00' + (({h2} % 31536000) || ' seconds')::interval
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("audit_log", 0.03, True, min_rows=60, depends=("app_user",), ddl=f"""
CREATE TABLE {S}.audit_log (
  id          BIGINT PRIMARY KEY,
  actor_id    BIGINT REFERENCES {S}.app_user(id),
  action      VARCHAR(60) NOT NULL,
  target_kind VARCHAR(40),
  target_id   BIGINT,
  ip_address  INET,
  diff        JSONB,
  logged_at   TIMESTAMPTZ NOT NULL
);""", fill=f"""
INSERT INTO {S}.audit_log
SELECT g, CASE WHEN g % 13 = 0 THEN NULL ELSE ({h1} % {{app_user}}) + 1 END,
       (ARRAY['create','update','delete','login','export'])[(g % 5) + 1],
       CASE WHEN g % 3 = 0 THEN NULL ELSE (ARRAY['org','user','invoice','order'])[(g % 4) + 1] END,
       CASE WHEN g % 3 = 0 THEN NULL ELSE ({h2} % 100000) END,
       CASE WHEN g % 11 = 0 THEN NULL
            WHEN g % 7 = 0 THEN ('2001:db8::' || to_hex(({h1} % 65536)))::inet
            ELSE ('10.' || (({h1} / 65536) % 256) || '.' || (({h1} / 256) % 256) || '.' || ({h1} % 256))::inet END,
       CASE WHEN g % 2 = 0 THEN NULL ELSE jsonb_build_object('before', ({h1} % 100), 'after', ({h2} % 100)) END,
       timestamptz '2024-01-01 00:00:00+00' + (({h1} % 31536000) || ' seconds')::interval
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("feature_flag", 0.00005, True, min_rows=30, ddl=f"""
CREATE TABLE {S}.feature_flag (
  id          BIGINT PRIMARY KEY,
  key         VARCHAR(80) NOT NULL,
  enabled     BOOLEAN NOT NULL,
  rollout_pct NUMERIC(5,2) NOT NULL,
  rules       JSONB,
  updated_at  TIMESTAMPTZ NOT NULL
);""", fill=f"""
INSERT INTO {S}.feature_flag
SELECT g, 'flag_' || g, g % 3 <> 0, (({h1} % 10001)::numeric / 100),
       CASE WHEN g % 2 = 0 THEN NULL ELSE jsonb_build_object('orgs', jsonb_build_array(({h1} % 100), ({h2} % 100))) END,
       timestamptz '2024-01-01 00:00:00+00' + (g || ' hours')::interval
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("webhook_endpoint", 0.001, True, min_rows=20, depends=("org",), ddl=f"""
CREATE TABLE {S}.webhook_endpoint (
  id         BIGINT PRIMARY KEY,
  org_id     BIGINT NOT NULL REFERENCES {S}.org(id),
  url        VARCHAR(2048) NOT NULL,
  secret_hash CHAR(64) NOT NULL,
  is_active  BOOLEAN NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);""", fill=f"""
INSERT INTO {S}.webhook_endpoint
SELECT g, ({h1} % {{org}}) + 1, 'https://hooks.example.com/' || g,
       lpad(to_hex({h1}), 64, '0'), g % 4 <> 0,
       timestamptz '2024-01-01 00:00:00+00' + (g || ' minutes')::interval
FROM generate_series(1::bigint, {{n}}) g;"""))

    E.append(Entity("webhook_delivery", 0.02, True, min_rows=40, depends=("webhook_endpoint",), ddl=f"""
CREATE TABLE {S}.webhook_delivery (
  id           BIGINT PRIMARY KEY,
  endpoint_id  BIGINT NOT NULL REFERENCES {S}.webhook_endpoint(id),
  status_code  INTEGER,
  attempt      SMALLINT NOT NULL,
  payload      JSONB NOT NULL,
  delivered_at TIMESTAMPTZ
);""", fill=f"""
INSERT INTO {S}.webhook_delivery
SELECT g, ({h1} % {{webhook_endpoint}}) + 1,
       CASE WHEN g % 9 = 0 THEN NULL ELSE (ARRAY[200, 201, 400, 404, 500, 503])[(g % 6) + 1] END,
       ((g % 5) + 1)::smallint,
       jsonb_build_object('event', 'e' || ({h1} % 1000), 'n', g),
       CASE WHEN g % 9 = 0 THEN NULL ELSE timestamptz '2024-01-01 00:00:00+00' + (({h2} % 31536000) || ' seconds')::interval END
FROM generate_series(1::bigint, {{n}}) g;"""))

    # ── no unique id: the four entities the ordering defect lives in ─────────
    E.append(Entity("page_view", 0.10, False, min_rows=200, ddl=f"""
CREATE TABLE {S}.page_view (
  session_id  BIGINT NOT NULL,
  path        VARCHAR(512) NOT NULL,
  viewed_at   TIMESTAMP NOT NULL,
  duration_ms INTEGER,
  referrer    VARCHAR(512)
);""", fill=f"""
INSERT INTO {S}.page_view
SELECT ({h1} % 100000) + 1, '/p/' || ({h2} % 5000),
       timestamp '2024-01-01 00:00:00' + (({h1} % 31536000) || ' seconds')::interval,
       CASE WHEN g % 5 = 0 THEN NULL ELSE ({h2} % 60000) END,
       CASE WHEN g % 3 = 0 THEN NULL ELSE 'https://ref' || ({h1} % 100) || '.example.org' END
FROM generate_series(1::bigint, {{n}}) g;""", note="no id, no PK — append-only clickstream"))

    E.append(Entity("metric_sample", 0.06, False, min_rows=200, ddl=f"""
CREATE TABLE {S}.metric_sample (
  metric_name VARCHAR(120) NOT NULL,
  sampled_at  TIMESTAMPTZ NOT NULL,
  value       DOUBLE PRECISION NOT NULL,
  tags        TEXT[]
);""", fill=f"""
INSERT INTO {S}.metric_sample
SELECT 'm.' || ((g / 97) % 500),
       timestamptz '2024-01-01 00:00:00+00' + (((g / 97) * 60) || ' seconds')::interval,
       (({mx.h1('(g / 97)')} % 1000000)::double precision / 1000),
       CASE WHEN (g / 97) % 4 = 0 THEN NULL ELSE ARRAY['host' || ((g / 97) % 20)] END
FROM generate_series(1::bigint, {{n}}) g;""",
        note="no id AND exact-duplicate rows: every value is a function of g/97, so each row "
             "appears 97 times — the multiset has repeats, so 'sort by every column' is not a "
             "sufficient fix for the ordering defect either"))

    E.append(Entity("api_request_log", 0.04, False, min_rows=200, ddl=f"""
CREATE TABLE {S}.api_request_log (
  token_id     BIGINT,
  method       VARCHAR(8) NOT NULL,
  path         VARCHAR(512) NOT NULL,
  status_code  SMALLINT NOT NULL,
  latency_ms   INTEGER NOT NULL,
  requested_at TIMESTAMPTZ NOT NULL,
  client_ip    INET
);""", fill=f"""
INSERT INTO {S}.api_request_log
SELECT CASE WHEN g % 17 = 0 THEN NULL ELSE ({h1} % 5000) + 1 END,
       (ARRAY['GET','POST','PUT','DELETE'])[(g % 4) + 1], '/v1/r/' || ({h2} % 300),
       ((ARRAY[200, 201, 204, 400, 401, 404, 429, 500])[(g % 8) + 1])::smallint,
       ({h1} % 5000),
       timestamptz '2024-01-01 00:00:00+00' + (({h2} % 31536000) || ' seconds')::interval,
       CASE WHEN g % 11 = 0 THEN NULL ELSE ('192.0.2.' || ({h1} % 256))::inet END
FROM generate_series(1::bigint, {{n}}) g;""", note="no id — SMALLINT in volume, INET in volume"))

    # `event` is the dominant entity: share is a placeholder, it absorbs the remainder.
    E.append(Entity("event", 0.0, False, min_rows=1000, ddl=f"""
CREATE TABLE {S}.event (
  org_id        BIGINT NOT NULL,
  user_id       BIGINT,
  event_type    {S}.event_kind NOT NULL,
  occurred_at   TIMESTAMPTZ NOT NULL,
  props         JSONB,
  session_token VARCHAR(64)
);""", fill=f"""
INSERT INTO {S}.event
SELECT ({h1} % {{org}}) + 1,
       CASE WHEN g % 19 = 0 THEN NULL ELSE ({h2} % {{app_user}}) + 1 END,
       (ARRAY['click','view','signup','purchase','error']::{S}.event_kind[])[(g % 5) + 1],
       timestamptz '2024-01-01 00:00:00+00' + (({h1} % 31536000) || ' seconds')::interval
         + ((g % 1000) || ' milliseconds')::interval,
       CASE WHEN g % 3 = 0 THEN NULL
            ELSE jsonb_build_object('k', ({h2} % 100), 'v', 'val' || ({h1} % 10000), 'ok', g % 2 = 0) END,
       CASE WHEN g % 23 = 0 THEN NULL ELSE lpad(to_hex({h2}), 16, '0') END
FROM generate_series(1::bigint, {{n}}) g;""", note="no id — ≥50% of all rows; the ≥5M-row entity at gate tier"))
    return E


# ── hand-authored trap tables (VALUES only — bullet 0 audits every literal) ──

def _trap_ddl(schema: str) -> str:
    S = schema
    return f"""
-- ── Hand-authored trap tables (SAU-532). VALUES-only so bullet 0 can audit each
-- literal against what the engine stored. Volume will not find these; only deliberate
-- values will. Types Phase 0 never seeded: BYTEA, UUID, INTERVAL, MONEY, XML, TIME,
-- SMALLINT. Known categories are planted alongside so bullet 4's ablation still has
-- traps to inject in this corpus.
CREATE TABLE {S}.attachment (
  id          BIGINT PRIMARY KEY,
  upload_id   UUID NOT NULL,                 -- trap: UUID — canonical 36-char text, never NUMBER
  content     BYTEA NOT NULL,                -- trap: BYTEA — raw bytes, hex on the wire, BINARY target
  sha256      CHAR(64) NOT NULL,             -- known: fixed-width hex identity
  created_at  TIMESTAMPTZ NOT NULL
);
CREATE TABLE {S}.sla_policy (
  id               BIGINT PRIMARY KEY,
  name             VARCHAR(80) NOT NULL,
  response_within  INTERVAL NOT NULL,        -- trap: INTERVAL — no native Snowflake type
  business_start   TIME NOT NULL,            -- trap: TIME — time-of-day, not a timestamp
  business_end     TIME NOT NULL,
  escalation_after INTERVAL
);
CREATE TABLE {S}.ledger_entry (
  id           BIGINT PRIMARY KEY,
  amount       MONEY NOT NULL,               -- trap: MONEY — locale-formatted text on the wire ('$1,234.57')
  amount_exact NUMERIC(14,4) NOT NULL,       -- known: high-precision currency → NUMBER(14,4)
  posted_at    TIMESTAMP NOT NULL,           -- known: NTZ
  memo_last4   CHAR(4)                       -- known: fixed-width PII, leading zeros
);
CREATE TABLE {S}.legacy_document (
  id          BIGINT PRIMARY KEY,
  body        XML NOT NULL,                  -- trap: XML — no equality operator, no native target
  doc_ref     VARCHAR(32),                   -- known: legacy numeric-looking text
  imported_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE {S}.device_reading (
  id        BIGINT PRIMARY KEY,
  sensor_no SMALLINT NOT NULL,               -- trap: SMALLINT — int2 range incl. -32768
  reading   REAL,                            -- known: narrow float, lossy
  read_at   TIMESTAMPTZ NOT NULL,
  tags      TEXT[]                           -- known: array
);
CREATE TABLE {S}.tax_rate_history (
  id             BIGINT PRIMARY KEY,
  region_code    CHAR(2) NOT NULL,
  rate           NUMERIC(9,6) NOT NULL,      -- known: precision-sensitive rate
  effective_from DATE NOT NULL,
  effective_to   DATE
);
"""


def _trap_seed(schema: str) -> str:
    """The VALUES-only trap corpus. Parsed by bullet 0 under the `postgres` grammar."""
    S = schema
    return f"""
INSERT INTO {S}.attachment (id, upload_id, content, sha256, created_at) VALUES
  (1, '00000000-0000-0000-0000-000000000001', '\\x00', '0000000000000000000000000000000000000000000000000000000000000001', '2024-01-01 00:00:00+00'),
  (2, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', '\\x0001fe ff', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', '2024-06-15 12:34:56+02'),
  (3, 'FFFFFFFF-FFFF-4FFF-8FFF-FFFFFFFFFFFF', '\\xdeadbeef00000000cafebabe', '00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff', '2024-12-31 23:59:59.999999+09'),
  (4, '12345678-1234-1234-1234-123456789012', '\\x', '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', '2024-03-10 08:00:00-05');

INSERT INTO {S}.sla_policy (id, name, response_within, business_start, business_end, escalation_after) VALUES
  (1, 'standard', '4 hours', '09:00:00', '17:00:00', '1 day'),
  (2, 'premium', '30 minutes', '00:00:00', '23:59:59.999999', '2 hours 30 minutes'),
  (3, 'enterprise', '1 day 02:03:04.5', '08:30:00.5', '18:15:00', NULL),
  (4, 'legacy', '1 year 2 months 3 days', '12:00:00', '12:00:00', '-1 hour');

INSERT INTO {S}.ledger_entry (id, amount, amount_exact, posted_at, memo_last4) VALUES
  (1, '1234.56', 1234.5600, '2024-01-01 00:00:00', '0042'),
  (2, '0.01', 0.0001, '2024-02-29 23:59:59.123456', '0007'),
  (3, '-99999.99', -99999.9999, '2024-07-04 12:00:00', NULL),
  (4, '1000000.00', 1000000.0000, '1999-12-31 23:59:59', '1234');

INSERT INTO {S}.legacy_document (id, body, doc_ref, imported_at) VALUES
  (1, '<doc id="1"><title>Alpha</title></doc>', '000123', '2024-01-01 00:00:00+00'),
  (2, '<doc id="2"><body>caf&#233; &amp; co</body><empty/></doc>', '4,50', '2024-05-05 05:05:05+00'),
  (3, '<a><b><c>nested</c></b></a>', '1e5', '2024-09-09 09:09:09+00'),
  (4, '<x/>', NULL, '2024-11-11 11:11:11+00');

INSERT INTO {S}.device_reading (id, sensor_no, reading, read_at, tags) VALUES
  (1, 0, 0.1, '2024-01-01 00:00:00+00', ARRAY['a']),
  (2, -32768, -1.5, '2024-01-02 00:00:00+00', ARRAY['b', 'c']),
  (3, 32767, 3.14159274, '2024-01-03 00:00:00+00', NULL),
  (4, 7, NULL, '2024-01-04 00:00:00+00', ARRAY[]::text[]);

INSERT INTO {S}.tax_rate_history (id, region_code, rate, effective_from, effective_to) VALUES
  (1, 'CA', 0.072500, '2020-01-01', '2023-12-31'),
  (2, 'NY', 0.088750, '2021-06-01', NULL),
  (3, 'TX', 0.000000, '2019-01-01', NULL),
  (4, 'WA', 0.104000, '2024-01-01', '2024-06-30');
"""


TRAP_ENTITIES = ("attachment", "sla_policy", "ledger_entry", "legacy_document",
                 "device_reading", "tax_rate_history")
TRAP_TYPES = ("BYTEA", "UUID", "INTERVAL", "MONEY", "XML", "TIME", "SMALLINT")


# ── assembly ────────────────────────────────────────────────────────────────

def _enum_ddl(schema: str) -> str:
    S = schema
    return f"""
CREATE TYPE {S}.user_role           AS ENUM ('owner', 'admin', 'member', 'billing', 'readonly');
CREATE TYPE {S}.subscription_status AS ENUM ('trialing', 'active', 'past_due', 'canceled', 'paused');
CREATE TYPE {S}.order_status        AS ENUM ('cart', 'placed', 'paid', 'fulfilled', 'refunded', 'canceled');
CREATE TYPE {S}.payment_method      AS ENUM ('card', 'ach', 'wire', 'paypal', 'credit');
CREATE TYPE {S}.ticket_priority     AS ENUM ('low', 'normal', 'high', 'urgent');
CREATE TYPE {S}.currency_code       AS ENUM ('USD', 'EUR', 'GBP', 'JPY', 'VND');
CREATE TYPE {S}.event_kind          AS ENUM ('click', 'view', 'signup', 'purchase', 'error');
"""


def row_plan(total_rows: int, entities: list[Entity]) -> dict[str, int]:
    """Rows per entity: floor(share × total) floored at `min_rows`; `event` absorbs the
    remainder so the total is exactly `total_rows`. Raises if the shape cannot hold."""
    plan: dict[str, int] = {}
    for e in entities:
        if e.name == "event":
            continue
        plan[e.name] = max(e.min_rows, int(total_rows * e.share))
    used = sum(plan.values())
    plan["event"] = total_rows - used
    if plan["event"] < used or plan["event"] < 1000:
        raise SystemExit(
            f"--rows {total_rows} is too small for the shape: the dominant entity would "
            f"hold {plan['event']} of {total_rows} rows (< 50%). Use ≥ 20 000.")
    return plan


@dataclass
class Corpus:
    seed: int
    schema: str
    total_rows: int
    plan: dict[str, int]
    entities: list[Entity]
    ddl_sql: str
    volume_sql: str
    trap_sql: str
    mix: Mix = field(repr=False)

    @property
    def full_sql(self) -> str:
        return self.ddl_sql + self.volume_sql + self.trap_sql

    def manifest(self) -> dict[str, Any]:
        n_trap = len(TRAP_ENTITIES)
        trap_rows = 4 * n_trap
        dominant = max(self.plan, key=self.plan.get)
        return {
            "issue": "SAU-532",
            "seed": self.seed,
            "schema": self.schema,
            "total_rows": self.total_rows + trap_rows,
            "volume_rows": self.total_rows,
            "trap_rows": trap_rows,
            "sha256_sql": hashlib.sha256(self.full_sql.encode()).hexdigest(),
            "sha256_trap_sql": hashlib.sha256(self.trap_sql.encode()).hexdigest(),
            "mix": {"a1": self.mix.a1, "a2": self.mix.a2, "b1": self.mix.b1, "b2": self.mix.b2},
            "shape": {
                "entities": len(self.entities) + n_trap,
                "dominant_entity": dominant,
                "dominant_rows": self.plan[dominant],
                "dominant_share": round(self.plan[dominant] / self.total_rows, 4),
                "entities_without_unique_id": [e.name for e in self.entities if not e.has_id],
                "trap_types": list(TRAP_TYPES),
                "trap_entities": list(TRAP_ENTITIES),
            },
            "entities": [
                *({"name": e.name, "rows": self.plan[e.name], "has_unique_id": e.has_id,
                   "kind": "volume", "note": e.note} for e in self.entities),
                *({"name": t, "rows": 4, "has_unique_id": True, "kind": "trap", "note": ""}
                  for t in TRAP_ENTITIES),
            ],
        }


def build(total_rows: int, *, seed: int = DEFAULT_SEED, schema: str = DEFAULT_SCHEMA) -> Corpus:
    mx = Mix.from_seed(seed)
    ents = _volume_entities(mx, schema)
    plan = row_plan(total_rows, ents)
    # Topological order for FKs: dependencies are declared, so a stable sort suffices.
    ordered: list[Entity] = []
    seen: set[str] = set()
    pending = list(ents)
    while pending:
        progressed = False
        for e in list(pending):
            if all(d in seen for d in e.depends):
                ordered.append(e); seen.add(e.name); pending.remove(e); progressed = True
        if not progressed:
            raise RuntimeError("dependency cycle in corpus entities")
    header = (f"-- SAU-532 Phase-1 corpus — seed={seed} rows={total_rows} schema={schema}\n"
              f"-- GENERATED by corpus/generate.py; every value is a pure function of "
              f"(seed, ordinal). Do not hand-edit.\n"
              f"DROP SCHEMA IF EXISTS {schema} CASCADE;\nCREATE SCHEMA {schema};\n")
    ddl = header + _enum_ddl(schema) + "".join(e.ddl + "\n" for e in ordered) + _trap_ddl(schema)
    counts = {**plan}
    volume = "".join(
        f"\n-- {e.name}: {plan[e.name]} rows" + (f" ({e.note})" if e.note else "") + "\n"
        + e.fill.format(n=plan[e.name], **counts) + "\n"
        for e in ordered
    )
    analyze = "".join(f"ANALYZE {schema}.{e.name};\n" for e in ordered)
    analyze += "".join(f"ANALYZE {schema}.{t};\n" for t in TRAP_ENTITIES)
    return Corpus(seed=seed, schema=schema, total_rows=total_rows, plan=plan, entities=ordered,
                  ddl_sql=ddl, volume_sql=volume + "\n" + analyze, trap_sql=_trap_seed(schema),
                  mix=mx)


# ── apply + in-database content digests ─────────────────────────────────────

def _psql(sql: str, *, context: str, stdin: str | None = None) -> str:
    cmd = ["psql", "-X", "-tA", "-v", "ON_ERROR_STOP=1"]
    cmd += ["-f", "-"] if stdin is not None else ["-c", sql]
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"{context} failed (exit {proc.returncode})")
    return proc.stdout


def content_digests(schema: str, entities: list[str]) -> dict[str, dict[str, Any]]:
    """Per-entity {rows, sha256} over rows sorted by EVERY column, computed in-database.

    Order-independent by construction — this is the corpus identity at rest and is
    deliberately *not* the reconciliation checksum (which folds the scan ordinal in).
    XML has no ordering operator, so the sort key is the row's text form.
    """
    out: dict[str, dict[str, Any]] = {}
    for t in entities:
        sql = (f"SELECT count(*) || '|' || coalesce(encode(sha256(string_agg(r, E'\\n' "
               f"ORDER BY r)::bytea), 'hex'), '') "
               f"FROM (SELECT t::text AS r FROM {schema}.{t} t) s")
        n, digest = _psql(sql, context=f"digest {t}").strip().split("|")
        out[t] = {"rows": int(n), "sha256": digest}
    return out


def apply(corpus: Corpus) -> dict[str, dict[str, Any]]:
    _psql("", context="load corpus", stdin=corpus.full_sql)
    return content_digests(corpus.schema, [e.name for e in corpus.entities] + list(TRAP_ENTITIES))


def main() -> int:
    ap = argparse.ArgumentParser(description="SAU-532 Phase-1 corpus generator")
    ap.add_argument("--rows", type=int, default=1_000_000,
                    help="volume rows (proxy tier 1M, gate tier 10M); trap rows are extra")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    ap.add_argument("--out", default=str(REPO / "artifacts" / "corpus"))
    ap.add_argument("--apply", action="store_true",
                    help="load into the Postgres named by the libpq env and record in-DB digests")
    args = ap.parse_args()

    corpus = build(args.rows, seed=args.seed, schema=args.schema)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "phase1_corpus.sql").write_text(corpus.full_sql)
    (out / "phase1_traps.sql").write_text(corpus.trap_sql)
    manifest = corpus.manifest()
    if args.apply:
        manifest["content_digests"] = apply(corpus)
        manifest["applied_to"] = {k: os.environ.get(k, "(default)")
                                  for k in ("PGHOST", "PGPORT", "PGDATABASE")}
    (out / "corpus_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    sh = manifest["shape"]
    print(f"[corpus] seed={corpus.seed} {sh['entities']} entities, "
          f"{manifest['total_rows']} rows, dominant {sh['dominant_entity']}="
          f"{sh['dominant_rows']} ({sh['dominant_share']:.0%}), "
          f"no-id: {', '.join(sh['entities_without_unique_id'])}")
    print(f"[corpus] sha256(sql)={manifest['sha256_sql']}")
    if args.apply:
        print(f"[corpus] applied → {manifest['applied_to']}; digests recorded")
    print(f"[corpus] → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
