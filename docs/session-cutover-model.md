# Session cutover model

`rust/pg-session-cutover-model` is the executable design of the urgent-event timing of step A3 of
the always-on plan: one orchestrator role on the digest wait of `plangraph state wait`
([state-shadow.md](state-shadow.md)). It is a pure Rust model beside
[pg-launch-recovery-model](../rust/pg-launch-recovery-model/README.md) and
[pg-session-admission-model](../rust/pg-session-admission-model/README.md): no I/O, no async, no
clock, no new dependency. **It switches nothing live**: no hook, unit, watchdog or launcher
changes, and no wait or wake path is removed until a reviewed design is accepted and a role has its
GO (rule 5.9). This slice (CUTOVER-MODEL-S1-URGENT-TIMING) holds the state, time, parameters and
invariants. Recycle, adoption, session slots, direct messages and the digest window are slice 2.

## Actors and state

One session (idle, in a turn, or stopped) with its wait (none, armed, fired, dead) and a queued
wake; sources (hung or not, last read); urgent events of six classes (future, occurred, detected
in a digest, received; age; escalated; alarmed); the supervisor; the role switch (off, shadow,
active) with its gates. `Model::apply` is one atomic transition; a refused event changes nothing.
`Tick` is 10 s and is refused while an obligation is due, so a scan, wake, takeover, alarm or
supervisor pass cannot be skipped by letting time pass. Time passes while a session works: only
the zero-time steps of a wake (fire the wait, begin the turn) are due at once.

## Time budgets (`Params`, `Budget`)

Each class (go row, grant answer, main red, failed or unknown run, lease margin, blocked or failed
to the role) has its own budget: `takeover`, `alarm` and `bound`, in seconds from occurrence. The
default is 40, 60 and 90 for every class until the A2 shadow latencies and the operator say
otherwise. `Params::check` refuses a bound above 90 s (rule 3.48, D-6578), a step that leaves the
supervisor less than one pass (20 s) before the next, times that are not whole ticks, and times
beyond 2000 s, which the counters would not hold. At most 64 events, two sources.

| Step | At the earliest | The supervisor must have done it by |
| --- | --- | --- |
| `Escalate`: today's wake path takes over, the session is woken | takeover age | takeover + 20 s |
| `Alarm`: the operator is told and holds the event | alarm age | alarm + 20 s, never after the bound |
| a source is read (`Scan`) | | every 60 s, a hung read is visible at once |

## Invariants (`Model::violations`)

| Invariant | Requirement of plan section 4 A3 |
| --- | --- |
| `UrgentBound`: no urgent event is unread past its class bound. A woken session that keeps working, a hung source, an escalated event and a dead role are not exempt: the alarm hands the event to the operator, which is a reader | 1, 4 |
| `Unattended`: an active role is never idle with nothing armed or woken for more than one supervisor pass | 4 |
| `ReceivedUnread`: an event is received only by a session's receipt or the alarm; a scan never acknowledges | 6 |
| `ActivationGate`: active needs shadow sign-off, a fence, deployed recovery and a live GO | 7, 7a |

`Activate` is refused (`Overdue`) while an urgent event is already past its alarm age, so a role
never starts, or restarts after a rollback, with its bound already broken. `Rollback` and
`RevokeGo` hand every unread event to today's wake path. A role that is not switched keeps today's
path: its events are escalated and the session woken at once.

## Findings as scenarios (`tests/scenarios_timing.rs`, `scenarios_switch.rs`)

| Finding | Scenario test |
| --- | --- |
| round 3 HIGH invariants.rs:50: a dead role's event received at 100 s | `a_dead_role_hands_the_event_to_the_operator_by_the_bound_not_at_recovery` |
| round 3 HIGH time.rs:155: a fired wake freezes time while the session works | `a_woken_session_that_keeps_working_does_not_freeze_time_...` |
| round 3 HIGH transition.rs:177: reactivation with an overdue event | `activation_is_refused_with_an_urgent_event_...`, `reactivation_after_a_rollback_...` |
| round 3 MED params.rs:26: one budget for all classes | `each_urgent_class_has_its_own_budget_...` |
| state r2 C3/R1, alwayson r2: hung adapter, failed wake, five-minute supervisor | `an_unread_event_on_a_hung_source_...`, `a_failed_wake_is_recovered_...`, `a_hung_adapter_does_not_delay_...`, `a_five_minute_supervisor_...` |
| alwayson r3: activation, rollback, per-role GO | `activation_needs_shadow_fence_...`, `rollback_and_a_lost_go_...` |

Each guard a finding named is a `Defect` (`NoAlarm`, `NoSupervisor`, `ActivateWithoutGates`,
`ActivateOverdue`); `each_defect_is_caught_by_its_invariant` shows the unsafe model breaks the
listed invariant while the same search on the safe model finds nothing.

## Exhaustive bounds (`tests/exhaustive.rs`)

Production times. Every ordering up to: wake, 2 events of two classes on two sources, activated
role, 14 events; classes, a go row with a 60 s budget and a lease margin, 12 events; gates, all
gate and mode events with 1 event, 15 events. Each checks that refusals change nothing,
invariants hold, event stages never go back, and time is never blocked with no way out.

## Not modelled (later slices and other items)

Recycle, adoption, session slots, direct-message carry, the digest window and its byte limit,
source adapters and event retention (A2), the journal (B), keeper generation issue (stage C),
external effect receipts (D). The alarm stands for the operator path; its delivery is a build item.
