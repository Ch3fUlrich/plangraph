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

Owner clear-on-reopen is the **only door to a new run**: reaper lease expiry,
release, review change requests 1 and 2, reopen, and withdraw clear the owner.
The `new_run` takeover is removed; a different run cannot replace a live owner.
Worker
verbs never take over ownership **while an owner exists**; an unowned card is
claimed by the first run-carrying verb that records a fact (not audit-only or
no-op calls). Same-run attempts may advance, never go backwards. However,
after reaping, a late call from the old run can reacquire ownership if it is the
first fact-writing call; clearing ownership does not retain a retired-run fence.
In particular,
complete on an unowned waiting card acquires the owner without changing its
state: the completion report is a fact, not a lifecycle state transition.

The projector does not yet forward run ids to `set_state`. Therefore the first
run-carrying worker verb wins ownership, rather than the projector establishing
the owner. The forward-only transition table applies to `set_state` with `run`.
The park forward-table block is removed as dead: every table state reaches
`parked`, so it supplies no state protection. The live-set CAS below supplies
that protection instead. Claim/lease guards and exec identity checks still apply.
A claim holder may still park without `expected_state`; this legacy lease path
does not authorize Temporal on a claimed card. Bare projector `set_state` does
not use the table; forward fencing awaits **RT-P1-PROJECTOR-RUN-IDS**.

An exec attempt must be an integer in `[1, 2**31-1]`; invalid attempts are
`validation` (HTTP 400). `worker_attempt` is a read-only check, with no write
transaction: it neither writes state nor acquires or advances ownership.

Worker park, complete and ask_question may carry optional `expected_state`, a
string or non-empty list of strings from `SETTABLE_STATES` (any-of). When supplied,
the card's current state must equal the string or be in the list, and the exec
identity/attempt checks must still pass. Every list element is validated.
The worker sends the live set `["open", "ready", "running", "needs_input"]` for
`ask_question`, question parks, and generic park. Complete and alarm send none.
The attempt report writes no state, so the card is still seeded `open` or `ready`
before the projector pass; asks and parks must work both then and after projection
to `needs_input`. The run/attempt fence protects against stale retries; state CAS
only prevents overwriting a newer actor's `waiting`, `parked`, `failed`, or `done`.
`waiting` and `claimed` are excluded. A worker-run card is never `claimed`:
that is the agent lease path, which the projector skips. Temporal on a claimed
card is a misconfiguration refused with `exec_stale_state` for live-set ask/park.
The state comparison runs inside the same write transaction, before any write
or ownership recording/attempt bump; mismatch refuses `exec_stale_state` (HTTP
409) without changing state, owner or `updated_at`. Null `expected_state` is
treated as omitted on ask, park and complete. Omitting it preserves the
historical behavior. The shared `_exec_expect_state(item, expected)` helper is
also the ask_question integration point.

## Rollout

Migrate first, then deploy keeper, then worker: on Postgres `open_db(create=False)`
refuses a schema behind the code (`keeper schema is at migration <have>, code needs
<latest>: run pg admin migrate`), so the keeper will not start before the
`pg admin migrate` step (`docs/keeper-postgres.md`); only then does a worker with a
known attempt send the exec fields. An old keeper silently drops them on
ask_question, but rejects
unknown fields on park/complete/alarm/worker_attempt. During the mixed
window, an old worker's park/complete/alarm/worker_attempt/ask_question without exec fields is refused
with `exec_stale` once a new worker owns the card.

## Consequences

Stale identities are refused while an owner exists, rather than silently taking
over lifecycle facts. After an owner-clearing reopen, first writer wins, even
if that writer is a late worker from the previous run (a known limit).
The observed-state compare-and-set closes the delayed-duplicate case within the
same run and attempt: after the projector moves running to waiting, a delayed
worker verb expecting running cannot overwrite waiting or record ownership.
This is not an exactly-once delivery guarantee: calls without an expectation,
or duplicates whose expected state still matches, retain existing semantics.
