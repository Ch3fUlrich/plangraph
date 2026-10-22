# ADR: keeper exec ownership

Status: Accepted (2026-10-22)

## Context

Worker calls can arrive after another Temporal run has begun. One owner per
fact keeps the keeper's lifecycle state from being shared between runs.

## Decision

The keeper records `(workflow_id, run_id, attempt)` on the first run-carrying
call that changes a fact: first writer wins. Audit-only attempts and no-op
calls do not acquire a first owner. An existing owner still requires stale
run and backwards-attempt checks, even on a no-op.

Only keeper reopen (reaper) or release frees the owner. Worker verbs never
take over ownership. Same-run attempts may advance, never go backwards.

Deploy keeper first, then worker: a worker with a known attempt sends the
exec fields, which an old keeper rejects as unknown fields.

## Consequences

Stale identities are refused rather than silently taking over lifecycle facts.
Ownership fences run identity, not duplicate delivery within the same run and
attempt; it is not an exactly-once delivery guarantee.
