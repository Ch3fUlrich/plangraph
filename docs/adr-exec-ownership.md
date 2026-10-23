# ADR: keeper exec ownership

Status: Accepted (2026-10-22)

## Follow-up: worker binding and revival

An unclaimed card defaults to refusing ownership recording. It must have a
delegate matching the worker principal; a claimed card requires its holder.
Router-only API/MCP `requeue` revives a parked card to open using the explicit
hand-back path. Store hand-back is restricted to router and system, not l1.
Run-less projector resumes from waiting or needs_input to running preserve the
owner. `new_run=True` is refused as validation, never silently ignored.

Owner recording, attempt advances and clears append `exec_owner_changed` events
inside their write transaction, so the hash chain attests these changes on both
backends. The retired-run fence retains only the last 16 run identities, keeping
the maximum attempt for each retained identity. Older evicted identities are no
longer fenced; this bounds per-card storage rather than promising eternal replay
protection.

## Context

Worker calls can arrive after another Temporal run has begun. One owner per
fact keeps the keeper's lifecycle state from being shared between runs.

## Decision

The keeper records `(workflow_id, run_id, attempt)` on the first run-carrying
call that changes a fact: first writer wins, and the owner is bound to the
principal that presented it. **Only a worker principal may record one** (the
worker that holds the card's Temporal workflow): the recording principal is
stored in `exec_principal` beside the triple; a router/l1/system/spawner token
presenting a triple on an unowned card is refused `forbidden` and records
nothing; on a claimed card the holder must be that principal, and an unclaimed
card requires a matching delegate. Once
recorded, a *worker* or *spawner* principal whose name differs from
`exec_principal` is refused `forbidden` on that card. Audit-only attempts and
no-op calls do not acquire a first owner. An existing owner still requires stale
run and backwards-attempt checks, even on a no-op. Audit-only calls with
`record=False` validate an existing owner without advancing its attempt.

Owner clear-on-reopen is the **only door to a new run**: reaper lease expiry,
release, review change requests 1 and 2, reopen, and withdraw clear the owner.
A `set_state` hand-back is the same door for lifecycle moves, but it is
explicit: only `set_state(..., hand_back=True)` that changes the state and
lands on `open`/`ready` clears the owner, in the same transaction as the state
update, so a parked card revives with a new run. A bare run-less `set_state` —
the projector/planner write path — is never a hand-back: it keeps the owner of
a live run (a run-carrying move, a no-op move and a move to any other state
keep the owner as well). The `new_run` takeover is refused; a different run cannot replace a
live owner.
Worker
verbs never take over ownership **while an owner exists**; an unowned card is
claimed by the first run-carrying verb that records a fact (not audit-only or
no-op calls). Same-run attempts may advance, never go backwards.
**Every clear keeps a retired-run fence**: the freed triple joins
`exec_retired_runs` (`workflow_id`, `run_id`, max `attempt`) in the same
transaction that drops the owner, so a late call from the run a door retired is
refused `exec_stale` — on an owned *and* on an unowned card — and can never be
the first writer at that card again. Reap, release, both review changes,
reopen, withdraw and the explicit `set_state` hand-back all clear through the
one `_exec_clear` helper, so the fence is written for all of them at once.
In particular,
complete on an unowned waiting card acquires the owner without changing its
state: the completion report is a fact, not a lifecycle state transition.
The gate, the record and the completion write of `worker_complete` (a
state/epoch CAS like `add_evidence`) share one write transaction, mirroring
park's single `_apply_state` transaction: a completion refused or lost to a
race rolls the record back, so a failed completion never leaves a recorded
owner.

The projector does not yet forward run ids to `set_state`. Therefore the first
run-carrying worker verb wins ownership, rather than the projector establishing
the owner. The forward-only transition table applies to `set_state` with `run`,
**and to a bare run-less `set_state` on an OWNED card**: a back move there
without `run`/`hand_back` is refused `bad_transition`, so router/l1/system (and
a late projection) can no longer walk an owned card backwards and strand it
open/ready-but-owned — the explicit `hand_back=True` door, or `release_claim`,
is the way back. Unowned cards keep the historical run-less behaviour. The
projector skips a state write the state machine refuses as a back move
(`states_skipped_state`) instead of failing the run: the card keeps the state
its own run left it in and the event is still marked applied. The park
forward-table block is removed as dead: every table state reaches `parked`, so
it supplies no state protection. The live-set CAS below supplies that
protection instead. Claim/lease guards and exec identity checks still apply.
A claim holder may still park without `expected_state`; this legacy lease path
does not authorize Temporal on a claimed card.

An exec attempt must be an integer in `[1, 2**31-1]`; invalid attempts are
`validation` (HTTP 400). `worker_attempt` is a read-only check, with no write
transaction: it neither writes state nor acquires or advances ownership.

Worker park, complete and ask_question may carry optional `expected_state`, a
string or non-empty list of strings from `SETTABLE_STATES` (any-of). When supplied,
the card's current state must equal the string or be in the list, and the exec
identity/attempt checks must still pass. Every list element is validated.
The worker sends the live set `["open", "ready", "running", "needs_input",
"waiting"]` for `ask_question`, question parks, generic park **and complete**
(`workflow.rs` `done_input`: the Rust complete sets `expected_state` to the
observed live set instead of `None`, so the real completion carries the same
CAS the keeper tests assume). Alarm still sends none. `waiting` belongs to the set: the projector writes `waiting`
after usage_limit/outage and on a timer, so the cap-park must still land on a
waiting card instead of being refused `exec_stale_state` on every retry. The
attempt report writes no state, so the card is still seeded `open` or `ready`
before the projector pass; asks and parks must work both then and after
projection to `needs_input`. The run/attempt fence protects against stale
retries; state CAS only prevents overwriting a newer actor's `parked`,
`failed`, or `done`, plus `waiting` for any expectation that does not name it
(the live set names it). `claimed` is excluded from the live set. A worker-run
card is never `claimed`: that is the agent lease path, which the projector
skips. Temporal on a claimed card is a misconfiguration refused with
`exec_stale_state` for live-set ask/park.
The state comparison runs inside the same write transaction, before any write
or ownership recording/attempt bump; mismatch refuses `exec_stale_state` (HTTP
409) without changing state, owner or `updated_at`. Null `expected_state` is
treated as omitted on ask, park and complete. Omitting it preserves the
historical behavior. The shared `_exec_expect_state(item, expected)` helper is
also the ask_question integration point.

Inside the park/complete guards the stale-run and lower-attempt checks decide
first (validating only), then a **duplicate park is a success no-op decided
before the state expectation**: if the card already reached `parked` and the
body names the recorded owner (or, on an unowned card, names no triple), a
retry whose response was lost succeeds rather than failing `exec_stale_state`
on every retry. A different run is still `exec_stale`, because that check runs
ahead of the duplicate decision. A duplicate complete against `done` is
ordered the same way.

## Storage

Two columns carry the binding and the fence, added by **PostgreSQL Migration 7**
(`schema_pg.py::_V7`, `MIGRATIONS`/`LATEST` = 7) and by the **SQLite v13**
path (`schema.py` items DDL + `db._ADDED_COLUMNS`, `SCHEMA_VERSION` = 13,
presence-based upgrade): `items.exec_principal TEXT` (the principal that
recorded the owner) and `items.exec_retired_runs TEXT` (JSON list of
`{workflow_id, run_id, attempt}` entries kept across owner clears), beside the
existing `exec_workflow_id`/`exec_run_id`/`exec_attempt`.

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
over lifecycle facts. After an owner-clearing reopen, first writer wins — but
only among runs the fence has not retired: the run the door cleared is refused
`exec_stale`, so a late worker from the previous run can no longer reacquire
the card, and the new run is the first writer.
The observed-state compare-and-set closes the delayed-duplicate case within the
same run and attempt: after the projector moves running to waiting, a delayed
worker verb expecting running cannot overwrite waiting or record ownership.
This is not an exactly-once delivery guarantee: calls without an expectation,
or duplicates whose expected state still matches, retain existing semantics.
# sq7 compatibility clarification

Requeue is an intentional router-only MCP tool: it revives a parked card and
retires its previous run. Discovery contains 34 tools; the existing size budget
still applies. A reaped or twice-rejected card must be claimed again before a
new worker can record its execution owner. Retired runs remain refused even
before that claim. No unclaimed first-writer takeover is restored.

`exec_owner_changed` is an explicitly handled lifecycle-fold no-op: ownership
audit does not change the lifecycle columns compared by that fold. Both storage
backends use the shared event-type registry. Historical event hashes are not
rewritten: the legacy replay opts out of the newer shared seeder's delegation
events, preserving its original operation sequence and unchanged hash pins.
