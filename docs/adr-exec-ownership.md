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

Worker park, complete and ask_question may carry optional `expected_state`, a
string from `SETTABLE_STATES`. When supplied, the card's current state must
equal that observed state and the exec identity/attempt checks must still pass.
The state comparison runs inside the same write transaction, before any write
or ownership recording/attempt bump; mismatch refuses `exec_stale_state` (HTTP
409) without changing state, owner or `updated_at`. Omitting it preserves the
historical behavior. The shared `_exec_expect_state(item, expected)` helper is
also the ask_question integration point.

Deploy keeper first, then worker: a worker with a known attempt sends the
exec fields, which an old keeper rejects as unknown fields.

## Consequences

Stale identities are refused rather than silently taking over lifecycle facts.
The observed-state compare-and-set closes the delayed-duplicate case within the
same run and attempt: after the projector moves running to waiting, a delayed
worker verb expecting running cannot overwrite waiting or record ownership.
This is not an exactly-once delivery guarantee: calls without an expectation,
or duplicates whose expected state still matches, retain existing semantics.
