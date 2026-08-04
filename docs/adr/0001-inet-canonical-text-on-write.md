# ADR-0001 — `inet`/`cidr` → `VARCHAR`: the migrated column carries the type's canonical text

- **Status**: Accepted
- **Date**: 2026-08-04
- **Decider**: CTO
- **Issue**: [SAU-517](/SAU/issues/SAU-517), arising from [SAU-516](/SAU/issues/SAU-516)
- **Scope**: the Postgres lane's `inet`/`cidr` types. The SQL Server lane's
  `audit_trail.ip_address` is a plain `VARCHAR` and is untouched by this.

## Context

`audit_log.ip_address` maps `no_native_target → VARCHAR(45)`. The source application
inserted `203.0.113.42`. Postgres stores the `inet` value whose canonical text is
`203.0.113.42/32`, and that text is what extraction reads and what lands in Snowflake.

Reconciliation passes, and it is right to pass: both sides read `/32`, the value is
identical under `inet` equality, nothing is lost. But the target column is a `VARCHAR`,
and under `VARCHAR` equality `203.0.113.42` and `203.0.113.42/32` are different strings.
A consumer of the migrated column receives a string the source application never wrote.

SAU-516 documented that on the mapping's contract (`core/mapper.py`, `SEM_NETWORK`) and
made it machine-readable as the flag `canonical_text_differs_from_input`. It deliberately
left one question open: should Phase 1 also **normalize on write** — land
`203.0.113.42` rather than `203.0.113.42/32`?

## Decision

**Keep the canonical form. Do not normalize on write.** The base column is the type's
canonical text, unconditionally. Any consumer needing the application's input form gets
it as a **read-time projection** on the target, never as a write-time policy in the
chassis.

## Why

**1. The two options were posed as a binary, and they are not one.** The framing assumed
a single column must serve both the archive and the consumer. It does not. Stripping the
host mask on write is irreversible — once `203.0.113.42/32` is gone, no downstream query
can recover whether the source held a host or a network. Stripping on *read* is always
available and costs nothing. When one direction of a transform is recoverable and the
other is not, the tool takes the recoverable one and leaves the choice to whoever owns
the consumer. That resolves the string-join concern raised in SAU-517 without paying for
it in losslessness.

(Note the narrow claim: this is a projection for *consumers*, not for *verification*.
Reconciliation and the §8 gate continue to anchor on the base column. A projection
standing in for a measurement is a separate failure mode and is not licensed here.)

**2. Canonical is a property of the source type; anything else is a property of our
tool.** `ip_address::text` is derivable from the Postgres type definition alone, by
anyone, without reading our source. A strip-on-write rule is derivable from nothing but
our own preference — it would exist only because we happened to notice the `/32` in a
readout. That is the same test SAU-516 applied to the equivalence rule and it fails it:
*would the rule have been written the same way before we saw this output?* No. A
migration chassis that accumulates per-column cosmetic policies becomes unauditable by
exactly the mechanism the §8 gate was built to catch.

**3. A blanket strip is straightforwardly lossy.** `host('10.0.0.0/24')` is `10.0.0.0` —
a network of 256 addresses flattened to one. The only safe variant is the narrow one
(strip iff the mask is exactly the host mask, `/32` or `/128`), which is the same narrow
scoping the equivalence rule already carries. That variant is *correct*, and it is still
rejected on grounds 1 and 2 — being safe is not the same as being warranted.

## Consequences

- `core/mapper.py` `SEM_NETWORK` and the Postgres `extraction_expr` stand as they are.
  No code change follows from this ADR.
- The `canonical_text_differs_from_input` flag is now the *contract*, not a caveat.
  Phase 1 target DDL should carry a normalized view or derived column for consumers who
  need the application-input form; that belongs to the target design, not the chassis.
- **This ruling was unenforceable when the ADR was written. SAU-518 closed that gap.**
  The gap, recorded as it stood: every `inet` row in `schema/02_seed.sql` was a bare host
  address (`203.0.113.42`, `198.51.100.7`, `::1`, `2001:db8::8a2e:370:7334`). Under a
  strip-on-write extraction, all four would match their seed literals *exactly*;
  `corpus.py` resolves equivalences only over the residual after the exact multiset diff,
  so the residual would be empty and gate bullet 0 would report zero missing, zero
  unclaimed, and zero equivalence applications — a **strictly greener** readout than
  today's, while the chassis silently acquired a lossy write policy. The manifest's
  `assert` string ("canonical text form preserved") is prose: `test_profile.py` checks
  only that the key exists. The unit tests pinned the *equivalence rule* against `/24`
  cases, but nothing pinned the *extraction*.

  What closed it, in the order the defect would have to get past:
  1. `schema/02_seed.sql` seeds a genuine network, `10.0.0.0/24`, whose canonical text
     equals no host-mask form. It matches the seed literal exactly and needs no
     equivalence — so a mask strip renders it `10.0.0.0`, matching nothing, and bullet 0
     goes RED. This is the tripwire the four host rows structurally could not be.
  2. `test_corpus.py` runs the strip as a live ablation and asserts **both** halves: that
     it is caught, *and* that all four host rows stay green under it — the counterfactual
     that makes the `/24` load-bearing rather than decorative.
  3. `test_dialect.py` pins the extraction expression itself to the canonical-text cast.
  4. The bullet-0 readout now states the direction, so an *emptier* equivalence table
     reads as the warning it is rather than as a cleaner result.

  Note what did **not** change: no strip-on-write normalization was implemented, and no
  seed literal was edited toward the engine's output form. The four host rows still claim
  the bare address and still reconcile by the named rule. Adding a `/24` is not the
  rejected option (a): a `/24` is not a form the engine canonicalized us into, it is a
  different value whose mask carries information no host address has.

## Alternatives considered

| Option | Verdict |
|---|---|
| Strip the host mask on write (blanket `host()`) | Rejected — lossy on any genuine network |
| Strip only the exact host mask (`/32`, `/128`) | Rejected — safe but tool-derived, not type-derived; irreversible |
| Land both a canonical and a host-only column | Rejected — column proliferation for a presentation concern a view already solves |
| Keep canonical, project on read | **Accepted** |
