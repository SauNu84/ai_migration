# ADR-0002 — `main` is a two-parent trunk: `sau-304` is merged, not dropped

- **Status**: Accepted
- **Date**: 2026-08-06
- **Decider**: CTO
- **Issue**: [SAU-538](/SAU/issues/SAU-538), arising from [SAU-524](/SAU/issues/SAU-524)
- **Scope**: repository trunk topology for `ai_migration`. Not a statement about what is
  shippable — see "What this ADR does not claim".

## Context

Until 2026-08-06 this repository **had no trunk**. Every branch was a lane branch off an
earlier lane branch, and no ref was the integration point. That is what made SAU-524
possible: SAU-321 read `done` because its work was committed to `sau-324-sqlserver-source`,
and there was no ref against which "committed" could be distinguished from "shippable".
Every §8 GREEN in the repo's history was, strictly, branch-local.

`df9717b` (`sau-324-sqlserver-source`) is the lane superset: it carries Phase 0 plus the
SQL Server lane, and both §8 readouts were produced from it. But it is **not** a repo-wide
superset. `aa55d25` (`sau-304-phase0-orchestration-bakeoff`) is not an ancestor of it, and
carries work reachable from nowhere else:

- `orchestration/RECOMMENDATION.md` — the Phase-0 Dagster-vs-Temporal decision record
- `tests/test_orchestration.py` — the tests backing it

Cutting a trunk at `df9717b` alone would have left `aa55d25` on an unreferenced branch.

## Decision

**`main` is cut from `df9717b` and merges `aa55d25`** — a two-parent merge commit
(`a578933`). `sau-304` is merged into the trunk, not dropped from it.

The `.gitignore` conflict was the only conflict, and it was resolved by keeping
`.venv-*/` (which strictly subsumes the incoming `.venv-orch/`) plus `.orch-tmp/`, which
both sides already carried. No ignore rule is lost in either direction.

## Why merge `sau-304` rather than drop it

**1. The asymmetry of the two mistakes.** If `orchestration/` turns out not to belong on
trunk, deleting it later is a one-line commit and the history stays reachable through the
merge parent. If `sau-304` is left off trunk and the first repo cleanup deletes the
unreferenced branch, the recommendation and its tests are gone — and, worse, so is any
marker that the decision was ever made. A later reader would not find an absent file;
they would find no evidence the bake-off happened and would re-run it. One direction is a
cheap reversible edit, the other destroys a decision record. Take the reversible one.

**2. A decision record is not code, and is not judged as code.** The question "should
`orchestration/` be part of the product?" is a live one. The question "should the repo
retain evidence that Dagster-vs-Temporal was evaluated and how?" is not. Merging answers
the second without prejudging the first.

**3. It costs the test run nothing.** `tests/test_orchestration.py` imports only `sys`,
`unittest`, `pathlib` and the local `common.medallion` — not `dagster`, not `temporalio`.
Merging `sau-304` therefore introduces no unmet dependency into the root test run, and
`orchestration/requirements.txt` does not need to merge into the root `requirements.txt`.
Verified by reading the file at `origin/sau-304`, not assumed.

## What this ADR does not claim

Cutting a trunk fixes the *topology* problem SAU-524 named. It does not make anything
shippable, and it must not be read as doing so. The §8 gate reconciles against a **DuckDB
execution proxy**; the Snowflake SQL is only *compile*-checked and there is no live
Snowflake in Phase 0. A GREEN gate on `main` means the transform is verified against a
proxy at an integration point — not that it has been executed against Snowflake.

The whole point of SAU-524 was that we stopped distinguishing "committed" from "shipped".
Replacing that with a subtler conflation — "green on trunk" for "shipped" — would be the
same error one step later.

Repointing the GitHub **default branch** to `main` is a separate, credential-authority
question and is not covered here.
