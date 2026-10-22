# ADR: keeper exec ownership

Status: Accepted (2026-10-22)

## Context

Worker calls can arrive after another Temporal run has begun. One owner per
fact keeps the keeper's lifecycle state from being shared between runs.

## Decision

The keeper records `(workflow_id, run_id, attempt)` on the first run-carrying
call that changes a fact: first writer wins. Audit-only attempts and no-op
calls do not acquire a first owner. An existing owner still requires stale
run and backwards-attempt checks, even on a no-op. Audit-only calls with
`record=False` validate an existing owner without advancing its attempt.

Keeper reopen (reaper), release, and review-reopen clear the owner. Worker
verbs never take over ownership **while an owner exists**; an unowned card is
claimed by the first run-carrying verb that records a fact (not audit-only or
no-op calls). Same-run attempts may advance, never go backwards. In particular,
complete on an unowned waiting card acquires the owner without changing its
state: the completion report is a fact, not a lifecycle state transition.

The projector does not yet forward run ids to `set_state`. Therefore the first
run-carrying worker verb wins ownership, rather than the projector establishing
the owner. The forward-only transition table applies to `set_state` with `run`
and worker park when the card is owned or the body carries exec fields (including
transitive reachability and same-state no-ops). Legacy unowned parks without exec
fields retain their old behavior. Bare projector `set_state` does not use the table;
forward fencing there awaits **RT-P1-PROJECTOR-RUN-IDS**.

A back-move through `set_state` may take over with `new_run=True` only when
`run_id` differs from the recorded run. Attempts are not compared across runs:
a new run restarts at attempt 1 and replaces the recorded attempt. The same
run_id with `new_run=True` cannot authorize a back-move. Worker verbs cannot
request this takeover.

Worker park, complete and ask_question may carry optional `expected_state`, a
string or non-empty list of strings from `SETTABLE_STATES` (any-of). When supplied,
the card's current state must equal the string or be in the list, and the exec
identity/attempt checks must still pass. Every list element is validated.
The worker sends `["running", "needs_input"]` for asks and question parks because
the needs-input attempt report precedes the projector's 30-second state pass.
The state comparison runs inside the same write transaction, before any write
or ownership recording/attempt bump; mismatch refuses `exec_stale_state` (HTTP
409) without changing state, owner or `updated_at`. Omitting it preserves the
historical behavior. The shared `_exec_expect_state(item, expected)` helper is
also the ask_question integration point.

## Rollout

Deploy keeper first, then worker: a worker with a known attempt sends the
exec fields. An old keeper silently drops them on ask_question, but rejects
unknown fields on park/complete/alarm/worker_attempt. During the mixed
window, an old worker's park/complete/alarm/worker_attempt/ask_question without exec fields is refused
with `exec_stale` once a new worker owns the card.

## Consequences

Stale identities are refused rather than silently taking over lifecycle facts.
The observed-state compare-and-set closes the delayed-duplicate case within the
same run and attempt: after the projector moves running to waiting, a delayed
worker verb expecting running cannot overwrite waiting or record ownership.
This is not an exactly-once delivery guarantee: calls without an expectation,
or duplicates whose expected state still matches, retain existing semantics.
