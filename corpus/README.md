# Phase-1 corpus — the ungated half of the SAU-480 volume bar (SAU-532)

`generate.py` builds the corpus both Phase-1 tiers load. It exists so the **proxy tier**
can run today, without the Snowflake credential the **gate tier** waits on.

| tier  | volume rows | where it runs        | closes                    |
|-------|-------------|----------------------|---------------------------|
| proxy | 1 000 000   | DuckDB (proxy)       | nothing — validates the generator and the loop |
| gate  | 10 000 000  | live Snowflake       | SAU-310 (blocked on SAU-308) |

Same shape at both tiers; only `--rows` changes.

## Shape (SAU-480 — the total alone is under-specified)

| requirement | this corpus |
|---|---|
| ≥ 25 entities | **27** (21 volume + 6 hand-authored trap tables) |
| ≥ 50 % of rows in one entity | `event` = **55 %** (550 940 at 1M; 5 509 400 at 10M) |
| ≥ 3 entities with no unique `id` | **4** — `event`, `page_view`, `api_request_log`, `metric_sample` |
| byte-identical across runs | SQL SHA-256 + per-entity in-database content digests |
| the 7 trap types Phase 0 never seeded | `BYTEA` `UUID` `INTERVAL` `MONEY` `XML` `TIME` `SMALLINT` — VALUES-only rows |

`metric_sample` additionally holds **exact-duplicate rows** (each distinct row appears 97
times), so the multiset has repeats: "sort by every column" is not a sufficient fix for
the ordering defect below.

## Why synthesized, and why the traps are separate from the volume

CI cannot hold customer rows. And real data *dilutes* trap density: a million production
rows contain fewer deliberate edge cases per row than a hundred hand-authored ones. So
volume is generated in-database (`generate_series` with seed-derived mixing constants —
no `random()`, no `now()`, no sequences), and the traps are literal `INSERT … VALUES`
rows in their own small tables. That split is also what makes bullet 0 work: it audits
what an INSERT *claims* against what the engine *stored*, and an `INSERT … SELECT`
claims nothing.

## The defect this corpus is built to catch

`core/reconcile.py::_gold_sql` orders by `"id"` only when the entity has one, and the
checksum folds the row ordinal in, so it is order-sensitive. An entity with no unique
`id` gets whatever scan order each engine produces — deterministic at 58 rows, not
guaranteed at 500k. The source walk and the Gold walk can then disagree with every value
intact: a flaky dirty verdict indistinguishable from real corruption. Four of the
entities above have no `id` on purpose. **If a run surfaces it, the bar is not lowered
to make it green.**

## Usage

```bash
# emit SQL + manifest only (no database needed)
python3 corpus/generate.py --rows 1000000 --seed 20260921 --out artifacts/corpus

# load into the Postgres named by the libpq env and record in-DB content digests
PGHOST=/tmp PGPORT=5599 PGUSER=postgres PGDATABASE=phase0 \
  python3 corpus/generate.py --rows 1000000 --apply

# profile + §8 gate on it (the extraction SQL is unqualified, so reach the schema via search_path)
export PGOPTIONS="-c search_path=phase1,public"
python3 profile/profile.py --schema phase1 --out artifacts/profile.phase1.json
python3 run_loop.py --profile artifacts/profile.phase1.json \
                    --manifest ground_truth/hard_columns.phase1.json \
                    --seed artifacts/corpus/phase1_traps.sql \
                    --readout GATE_READOUT.phase1.md --out artifacts/phase1
```

Outputs under `--out`: `phase1_corpus.sql` (DDL + volume fills + traps), `phase1_traps.sql`
(the VALUES-only file bullet 0 audits), `corpus_manifest.json` (seed, mixing constants,
row plan, shape, SHA-256 of the SQL, and with `--apply` the per-entity content digests
and the rig they were computed on).

Tests: `python3 -m unittest tests.test_phase1_corpus` (pure-Python tier always runs;
`SAU532_DB_TESTS=1` adds the load-twice-compare-digests tier against a reachable Postgres).

## What "byte-identical" means here

Two runs are the same corpus iff (a) the emitted SQL has the same SHA-256 and (b) every
entity's in-database content digest matches. (b) is computed over rows **sorted by every
column**, so it is order-independent by construction — which is precisely the property
the reconciliation checksum does *not* have, and why the two numbers are kept apart.

## 1M proxy-tier result (2026-09-21, seed 20260921)

Chassis: macOS 15.7.9 x86_64, Python 3.11.12, PostgreSQL 14.17 on `/tmp:5599`, DuckDB 1.5.5,
commit `90ce9a5`. 1 000 024 rows; load into Postgres 31 s; §8 gate (3 runs + ablation)
~42 min wall. **Not a target** — the bar is a volume, not a rate.

Verdict ❌ NO-GO (`GATE_READOUT.phase1.md`): bullet 0 (4 seed-vs-stored drifts) and bullet 3
(mapper has no rule for `uuid`/`bytea`/`interval`/`time`/`money`/`xml`) fail; 1, 2, 2b, 4 pass.

**Bullet 1 passing does NOT clear the ordering defect.** 3/3 clean at 1M with four no-`id`
entities holds only because both walks happen to return physical order: a fresh Postgres
heap seq-scans in insert order, and DuckDB defaults to `preserve_insertion_order=true`.
Probe at the `event` size (550 940 rows, CTAS then unordered SELECT, 8 threads, 5 runs each):
`preserve_insertion_order=true` → 1 digest; `false` → **5 distinct digests in 5 runs**.
Snowflake makes no insertion-order promise after a parallel `COPY INTO`, so the gate tier
will hit the defect that the proxy hides. The fix belongs in the loop, not in the corpus.

**Fixed in the loop by SAU-548.** A keyless entity is now reconciled with an
order-independent digest: an additive multiset hash per column (repeats counted, not
cancelled) plus a row-tuple multiset that keeps row alignment. Keyed entities keep the
`id`-ordered digest. See `core/reconcile.py::EntityDigest` and `tests/test_reconcile_order.py`.
