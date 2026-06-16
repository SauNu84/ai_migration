# SAU-304 — Dagster vs Temporal orchestration bake-off

Phase-0 decision spike (parent [SAU-302](https://github.com/SauNu84/ai_migration)).
Picks the orchestration runtime(s) for Phase 1 **before** build pressure forces a
default. The written recommendation is [`RECOMMENDATION.md`](RECOMMENDATION.md).

Both spikes run the **same** medallion work — Bronze (ingest profile) → Silver
(AI lift+map → candidate Snowflake mappings + confidence) → **human gate** on
below-floor mappings → Gold (codegen + reconcile) — so the comparison is about
orchestration mechanics, not differently-shaped toy programs. The shared,
orchestrator-agnostic domain logic is [`common/medallion.py`](common/medallion.py).

## Layout

```
common/medallion.py            shared Bronze/Silver/Gold + AI-mapping gate logic (no orchestrator imports)
dagster_spike/definitions.py   medallion as software-defined assets + asset-check gate
dagster_spike/run_dagster.py   headless runner: the two-step "split graph at the gate" pattern
temporal_spike/workflow.py     durable workflow: wait_condition gate + signal + query; activities
temporal_spike/run_temporal.py headless runner: start → block at gate → signal → resume (time-skipping)
requirements.txt               temporalio + dagster (pinned to validated versions)
RECOMMENDATION.md              the deliverable: which tool for which layer, and the Phase-1 default
```

## Run it

```bash
python3 -m venv .venv-orch
.venv-orch/bin/pip install -r orchestration/requirements.txt

# Dagster medallion spike (needs a writable DAGSTER_HOME)
DAGSTER_HOME=$PWD/.orch-tmp/dagster_home .venv-orch/bin/python orchestration/dagster_spike/run_dagster.py

# Temporal durable-gate spike (uses the in-process time-skipping test server; no cluster needed)
.venv-orch/bin/python orchestration/temporal_spike/run_temporal.py

# Gate-semantics acceptance test (fast, no dagster/temporal needed)
python3 -m unittest tests.test_orchestration -v
```

Inspect the Dagster lineage DAG in the UI:

```bash
DAGSTER_HOME=$PWD/.orch-tmp/dagster_home \
  .venv-orch/bin/dagster dev -f orchestration/dagster_spike/definitions.py
```

## What each spike demonstrates (validated 2026-06-16)

Both produce the **identical** outcome — 6 columns, 4 auto-approved, 2 held at the
gate (`accounts.tax_id` fixed-width PII, `servers.last_seen_ip` `inet` with no
native Snowflake type); the reviewer approves `tax_id`, rejects the `inet` →
**5/6 reconciled**:

| | Dagster | Temporal |
|---|---|---|
| Medallion model | Bronze/Silver/Gold as `@asset`s; lineage DAG derived for free | Bronze/Silver/Gold as activities; no lineage awareness |
| Below-floor signal | `@asset_check` (queryable, WARN severity) | workflow `@query` (`pending_columns`) |
| Human gate | **graph must be split**: materialize Bronze+Silver, review out-of-band, re-materialize Gold with decisions as run-config | **inline durable `wait_condition`**: one continuous workflow blocks for the human, resumes on a `@signal` |
| Durability of the wait | none — the "pause" is just two separate runs | full — survives worker/process restart, can wait days |

These two rows are the whole decision: Dagster owns *lineage*, Temporal owns the
*durable human wait*. See [`RECOMMENDATION.md`](RECOMMENDATION.md).
