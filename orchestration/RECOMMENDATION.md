# SAU-304 — Orchestration recommendation (Phase-0 bake-off)

**Decision:** **Both, split at their natural seam — but staged.**
**Dagster** owns the data plane (medallion Bronze/Silver/Gold + lineage).
**Temporal** owns the durable, human-gated agent plane.
**Phase-1 default: adopt Dagster now; pre-commit to Temporal (Cloud) for the
agent plane and introduce it only when the durable-wait trigger fires (below).**
This is pre-decided so we don't re-litigate it under build pressure.

Backed by two runnable spikes in this directory, both validated 2026-06-16 against
the *same* medallion + human-approval work (`common/medallion.py`):
`dagster_spike/` (dagster 1.12.1) and `temporal_spike/` (temporalio 1.18.2).
Both reach the identical outcome — 4 mappings auto-approved, 2 held at the
confidence gate, reviewer approves the fixed-width PII column and rejects the
`inet` (no native Snowflake type) → **5/6 reconciled**. They differ only in *how*
they orchestrate, which is the whole point.

---

## Why a split, not a winner

The system has two genuinely different workloads, and the spikes make the seam concrete:

1. **Data plane** — profile → lift → map → codegen → reconcile. Batch, asset-shaped,
   and the §8 story *is* provenance: which mapping produced which SQL, and does Gold
   reconcile. This is lineage-first work.
2. **Agent plane** — the AI-mapping approval loop: low-confidence mappings wait for a
   human SME (minutes to days), LLM/agent steps need per-step retry and compensation,
   and many such workflows run concurrently and must survive restarts. This is
   durable-execution work.

No single tool is good at both. Forcing one to do the other is where the cost shows up.

---

## Scorecard (from the spikes, not the brochures)

| Dimension | Dagster | Temporal | Edge |
|---|---|---|---|
| **Lineage** | Native. Bronze/Silver/Gold are `@asset`s; the DAG + per-asset metadata/checks come for free and render in the UI. Exactly the §8 provenance artifact. | None. Activities are opaque steps; you'd build lineage yourself. | **Dagster** (data plane) |
| **Human-in-the-loop fit** | Awkward. No in-run durable pause. The spike must **split the graph**: materialize Bronze+Silver, review *out-of-band*, then re-materialize Gold with decisions as run-config (two separate runs). | Native. The spike blocks inline on `workflow.wait_condition`, exposes `pending_columns` as a query, and resumes on a `submit_decision` **signal** — one continuous workflow. | **Temporal** (agent plane) |
| **Durability** | The "pause" isn't durable — it's just the absence of a run. State between steps lives in IO-managers/storage. | Durable execution is the core primitive: the wait survives worker/process death and can last days; history replays exactly. | **Temporal** |
| **Operational burden** | Local: `dagster dev`. Prod: webserver + daemon + a metadata Postgres. Moderate, well-trodden. | Self-hosted cluster (server + Cassandra/Postgres, optional Elasticsearch) is **heavy**. Temporal **Cloud** removes almost all of it for usage cost. | **Dagster** self-hosted; **tie** if Temporal Cloud |
| **Cost** | OSS; cost is the metadata DB + compute you already run. | OSS self-host = real ops headcount. Temporal Cloud = usage-based $ but near-zero ops. | **Dagster** at Phase-1 scale |
| **Retries / agent steps** | Op-level retries, but no compensation/saga model. | Per-activity retry policies, timeouts, heartbeats, saga compensation — built for flaky LLM/tool calls. | **Temporal** (agent plane) |
| **Scheduling / backfills / sensors** | First-class (schedules, sensors, partitioned backfills). | Cron schedules exist; no asset/partition/backfill concept. | **Dagster** (data plane) |

**One-line read:** Dagster owns *lineage + batch scheduling*; Temporal owns the
*durable human wait + agent-step reliability*. Each spike is clumsy at the other's job.

---

## Phase-1 default (the anti-re-litigation rule)

**Adopt Dagster now for the medallion pipeline.** It is the immediate Phase-1 need,
lineage is core to the §8 deliverable, and its operational burden is modest.

**Model the human-approval gate as a Dagster graph-split for now** (exactly the
two-step pattern in `dagster_spike/run_dagster.py`): materialize up to the gate,
record the human decision, materialize Gold. This costs **zero** second runtime
while approval latency is short and approvals don't need to outlive a process.

**Temporal is already chosen for the agent plane — just deferred.** Introduce it
(via **Temporal Cloud**, not self-hosted, to keep ops burden near zero) the moment
**any** of these triggers fire:

- **T1 — Durable wait:** approvals routinely outlive a single run/process, or carry
  multi-hour/day SLAs where a graph-split + re-trigger is operationally painful.
- **T2 — Agent-step reliability:** the mapping loop becomes multi-step agentic and
  needs per-step retry / timeout / compensation (saga) semantics for LLM/tool calls.
- **T3 — Concurrency at scale:** many long-lived approval workflows must run
  concurrently and survive restarts independently.

When a trigger fires, the seam is clean: Dagster materializes the data assets and
*hands the low-confidence set to a Temporal workflow* that owns the durable human
loop, then writes decisions back for the Gold materialization. The split modeled in
these spikes is the same split we'd ship.

**What we are explicitly NOT doing in Phase 1:** standing up a self-hosted Temporal
cluster, or running two orchestration control planes before the agent plane needs
durability. That is the over-engineering this decision exists to prevent.

---

## Risks & mitigations

- *Risk: graph-split human gate gets painful before a trigger formally fires.* —
  Treat T1 as a judgment call, not a checklist; if reviewers feel the two-step
  re-trigger friction weekly, that **is** T1. Mitigation: the spike already proves the
  Temporal path works in-process, so the migration is additive, not a rewrite.
- *Risk: Temporal Cloud cost/vendor lock.* — The workflow/activity code is portable to
  self-hosted Temporal (same SDK); Cloud-vs-self-host is an ops/cost decision we can
  revisit without touching workflow code.
- *Risk: two systems = two on-call surfaces.* — Mitigated by staging: only one control
  plane in Phase 1; the second arrives with Temporal Cloud (managed) when justified.
