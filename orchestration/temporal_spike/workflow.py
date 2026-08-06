"""Temporal spike — durable medallion workflow with a human-approval gate.

What this is meant to show for the bake-off:

  * Durable execution: the workflow runs Bronze -> Silver, then *blocks on a
    human decision* with `workflow.wait_condition(...)`. That wait is durable —
    the workflow can sleep for minutes or weeks; if the worker/process dies it
    resumes from history at exactly the wait, no state lost. This is the
    primitive Dagster lacks and the reason Temporal fits long-running,
    human-gated agent workflows.
  * Human-in-the-loop is a first-class signal (`submit_decision`) plus a query
    (`pending_columns`) — the gate is *inside one continuous workflow*, not a
    graph that must be split and re-launched out-of-band.
  * Business logic lives in Activities (retryable, side-effecting); the workflow
    is pure orchestration. Bronze/Silver/Gold are activities here — Temporal is
    not asset/lineage-aware, which is the tradeoff against Dagster for the data
    layer.

Run:  python orchestration/temporal_spike/run_temporal.py
(uses the time-skipping test server — no external Temporal cluster required).
"""

from datetime import timedelta
from typing import Dict, List

from temporalio import activity, workflow

# Domain logic is plain Python; let it through the workflow sandbox unchanged.
# The orchestration dir is placed on sys.path by the runner before import, so
# no (sandbox-restricted) path manipulation runs at workflow-module load time.
with workflow.unsafe.imports_passed_through():
    from dataclasses import asdict
    from common import medallion as md


# --------------------------------------------------------------------------- #
# Activities — side-effecting, retryable. They carry plain dicts across the
# boundary to stay converter-agnostic.
# --------------------------------------------------------------------------- #
@activity.defn
async def bronze_activity() -> List[dict]:
    return [asdict(c) for c in md.bronze_ingest()]


@activity.defn
async def silver_activity(columns: List[dict]) -> List[dict]:
    cols = [md.Column(**c) for c in columns]
    return [asdict(m) for m in md.silver_map(cols)]


@activity.defn
async def gold_activity(mappings: List[dict]) -> dict:
    maps = [md.Mapping(**m) for m in mappings]
    gold = md.gold_codegen_reconcile(maps)
    return {
        "gold": [asdict(g) for g in gold],
        "summary": md.summarize(maps, gold),
    }


# --------------------------------------------------------------------------- #
# Workflow — durable orchestration with the human gate inline.
# --------------------------------------------------------------------------- #
@workflow.defn
class MappingApprovalWorkflow:
    def __init__(self) -> None:
        self._decisions: Dict[str, bool] = {}
        self._pending: List[str] = []

    @workflow.run
    async def run(self, gate_timeout_seconds: int = 0) -> dict:
        opts = dict(start_to_close_timeout=timedelta(seconds=30))

        columns = await workflow.execute_activity(bronze_activity, **opts)
        mappings = await workflow.execute_activity(
            silver_activity, columns, **opts
        )

        # Which mappings need a human? (confidence below floor)
        self._pending = [m["column"] for m in mappings if m["approved"] is None]
        workflow.logger.info("held at gate: %s", self._pending)

        # DURABLE WAIT: block until every pending column has a decision.
        # Optionally time-bound it; on timeout, undecided columns fail closed.
        if self._pending:
            timeout = (
                timedelta(seconds=gate_timeout_seconds)
                if gate_timeout_seconds
                else None
            )
            try:
                await workflow.wait_condition(
                    lambda: not self._undecided(), timeout=timeout
                )
            except TimeoutError:
                workflow.logger.warning(
                    "gate timed out; failing closed on: %s", self._undecided()
                )

        # Apply human decisions (undecided -> rejected, fail-closed).
        for m in mappings:
            if m["approved"] is None:
                m["approved"] = self._decisions.get(m["column"], False)
                m["decided_by"] = "human"

        result = await workflow.execute_activity(gold_activity, mappings, **opts)
        return {
            "summary": result["summary"],
            "decisions": self._decisions,
            "gold": result["gold"],
        }

    def _undecided(self) -> List[str]:
        return [c for c in self._pending if c not in self._decisions]

    @workflow.signal
    async def submit_decision(self, column: str, approved: bool) -> None:
        """Human approval/rejection arrives as a durable signal."""
        self._decisions[column] = approved

    @workflow.query
    def pending_columns(self) -> List[str]:
        """A reviewer (or UI) asks what still needs a decision."""
        return self._undecided()
