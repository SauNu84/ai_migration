"""Headless runner for the Temporal medallion + human-gate spike.

Uses Temporal's time-skipping test environment, so it runs fully in-process
with NO external cluster. The flow mirrors a real human-in-the-loop run:

    start workflow  ->  it advances Bronze+Silver, then BLOCKS at the gate
    query pending   ->  reviewer sees exactly which columns await a decision
    send signals    ->  approvals/rejections delivered durably
    workflow resumes->  applies decisions, runs Gold, returns the summary

Run:  python orchestration/temporal_spike/run_temporal.py
Exits 0 when the workflow completes with Gold reconciled.
"""

import asyncio
import sys
from pathlib import Path

from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Put both the spike dir (for `workflow`) and the orchestration dir (for
# `common`) on sys.path BEFORE importing the workflow module — the Temporal
# sandbox re-imports it and forbids path manipulation at its module load.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))            # -> import workflow
sys.path.insert(0, str(_HERE.parent))     # -> from common import medallion
from workflow import (  # noqa: E402
    MappingApprovalWorkflow, bronze_activity, silver_activity, gold_activity,
)

TASK_QUEUE = "sau304-medallion"


async def main() -> int:
    print("=== Temporal spike: durable medallion + human gate ===\n")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        client: Client = env.client
        async with Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[MappingApprovalWorkflow],
            activities=[bronze_activity, silver_activity, gold_activity],
        ):
            # Start (don't await) so we can interact while it's parked at the gate.
            handle = await client.start_workflow(
                MappingApprovalWorkflow.run,
                args=[0],  # gate_timeout_seconds=0 -> wait indefinitely
                id="sau304-mapping-approval",
                task_queue=TASK_QUEUE,
            )
            print("started workflow; it will advance bronze+silver then block.\n")

            # Poll the durable query until the workflow has reached the gate.
            pending = []
            for _ in range(50):
                pending = await handle.query(MappingApprovalWorkflow.pending_columns)
                if pending:
                    break
                await asyncio.sleep(0.05)
            print(f"GATE    workflow blocked; awaiting decisions on: {pending}\n")

            # Human reviews out-of-band. Same policy as the Dagster spike:
            # approve the fixed-width VARCHAR, reject the inet.
            for col in pending:
                approved = not col.endswith("last_seen_ip")
                await handle.signal(
                    MappingApprovalWorkflow.submit_decision, args=[col, approved]
                )
                print(f"HUMAN   signal: {col} -> {'APPROVE' if approved else 'REJECT'}")
            print()

            # The wait_condition is now satisfied; the workflow resumes & finishes.
            result = await handle.result()
            print("RESUMED workflow completed.")
            print(f"        summary: {result['summary']}")
            skipped = [g["column"] for g in result["gold"] if not g["reconciled"]]
            print(f"        gold skipped: {skipped}\n")

    print("=== DONE (durable wait + signal; survives worker restart) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
