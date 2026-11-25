# Session cutover model

`rust/pg-session-cutover-model` is the executable design of the urgent-event timing of step A3 of
the always-on plan: one orchestrator role on the digest wait of `plangraph state wait`
([state-shadow.md](state-shadow.md)). It is a pure Rust model beside
[pg-launch-recovery-model](../rust/pg-launch-recovery-model/README.md) and
[pg-session-admission-model](../rust/pg-session-admission-model/README.md): no I/O, no async, no
clock, no new dependency. **It switches nothing live**: no hook, unit, watchdog or launcher
changes, and no wait or wake path is removed until a reviewed design is accepted and a role has its
GO (rule 5.9). This slice (CUTOVER-MODEL-S1-URGENT-TIMING) holds the state, time, parameters and
invariants. Recycle, adoption, session slots, direct messages and the digest window are the next
slice (CUTOVER-MODEL-S2-RECYCLE-ADOPT-SLOTS), as the brief of this slice states.

## Actors and state

One session (idle, in a turn, or stopped) with its wait (none, armed, fired, dead) and a queued
wake; sources (hung or not, last read); urgent events of six classes (future, occurred, detected
in a digest, received; age; escalated; alarmed); the supervisor; the role switch (off, shadow,
active) with its gates. `Model::apply` is one atomic transition; a refused event changes nothing.
`Tick` is 10 s and is refused while an obligation is due, so a scan, wake, takeover, alarm or
supervisor pass cannot be skipped by letting time pass. Time passes while a session works and
while a wake is queued: delivery can be late or lost (`WakeLost`), and the supervisor owes a pass.

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
| `Alarm`: the operator is told, tracked apart from any session receipt; the event stays pending for the session | alarm age | alarm + 20 s, never after the bound |
| `Nudge`: an idle session without an armed wait, or whose armed wait cannot fire because an event is unread past its takeover age, is woken again; in every mode while a binding event is unread | | every 20 s while it stays unattended |
| `Recover`: the authorized recovery of an idle or dead session that has not read a binding event; never of a working session | alarm age | by the bound: time stops at the bound until it is done |
| a source is read (`Scan`) | | every 60 s, a hung read is visible at once |

## Invariants (`Model::violations`)

| Invariant | Requirement of plan section 4 A3 |
| --- | --- |
| `UrgentBound`: the operator is told of every binding urgent event by its class bound, in any mode, unless a session has read it; a hung source, an escalated event and a dead role are not exempt | 1, 4 |
| `SessionBound`: an idle or dead session has read every binding event by its class bound, or the authorized recovery has replaced it; separate from the operator alarm. A session that was working when the bound passed is excused: never ended, its miss is reported | 1, 2 |
| `Unattended`: an active role is never idle with nothing armed or woken for more than one supervisor pass | 4 |
| `ReceivedUnread`: an event is received only by a session's receipt; a scan or an alarm never acknowledges | 6 |
| `ActivationGate`: active needs shadow sign-off, a fence, deployed recovery and a live GO | 7, 7a |

An event is binding when it occurs while the role is active, or is pending when the role is
switched on. Rollback and a lost GO do not release it: its takeover and alarm stay due in every
mode. `Activate` is refused (`Overdue`) while an event is past its alarm age without an alarm.
A role that is not switched keeps today's path: its events are escalated and the session woken.

**Session deadline.** An idle or dead session must have read a binding event by its bound, or
the authorized recovery (`Recover`, needing the deployed fence and recovery) has replaced it and
its successor read it: `SessionBound`, owed apart from the alarm. A working session is never ended
on a receipt timeout: if the bound passes while it works, the event is excused from recovery and
the miss is reported by `Model::misses` (`Unread` while no session has received it past the
bound, `Late` once a receipt came after it). A miss stays attached to the event across a mode
change, an alarm and the end of the session. `Recover` stands for the sequence of slice 2 (checked
handoff, launch, confirm, adopt); here its effect is the successor's first act, reading the event.

## Transitions

| Event | Guard | Effect |
| --- | --- | --- |
| `Occur(e)` | event not yet occurred | occurred, age 0; binding if active; otherwise escalated and the session woken |
| `Scan(s)` | role shadow or active | healthy: occurred events of `s` become detected; hung: `blind(s)`; never reads another source |
| `Hang(s)`, `Heal(s)` | | source `s` hangs or recovers |
| `WaiterFire` | active, wait armed, an event detected | wait fired, wake queued |
| `WaiterDie` | wait armed | wait dead |
| `WakeLost` | wake queued | wake dropped |
| `BeginTurn` | idle, wake queued | in a turn |
| `EndTurn` | in a turn | idle |
| `Arm` | in a turn, role active, wait not armed | wait armed |
| `Receive` | in a turn | every escalated event and, if active, every detected one: received, session receipt recorded |
| `Crash` | session live | stopped, wait dead |
| `Escalate(e)` | binding, pending, not escalated, takeover age | escalated, session woken |
| `Alarm(e)` | binding, pending, no alarm yet, alarm age | alarmed, operator time recorded; stays pending |
| `Nudge` | active, or a binding event unread; an idle session without an armed wait or with an unread event past its takeover age | wake queued, supervisor pass restarts |
| `Recover` | gates fence and recovery set; session idle or dead; a binding event pending at its alarm age | fresh session; every pending event received by it, escalated |
| `StartShadow`, `SignOffShadow` | off; shadow | shadow; sign-off |
| `DeployFence`, `DeployRecovery`, `GrantGo` | | gate set |
| `Activate` | shadow, all gates, no event past its alarm age unalarmed | active; pending events binding |
| `Rollback` | shadow or active | off; pending events escalated, session woken, wait dead; gates other than the sign-off stay |
| `RevokeGo` | any mode | GO cleared; if active, the same as `Rollback`; in shadow or off the mode stays and only the GO is gone |
| `Tick` | no obligation due: takeover, alarm, scan, supervisor pass | ages, scan and unattended clocks advance |

## Findings as scenarios (`tests/scenarios_timing.rs`, `scenarios_recovery.rs`, `scenarios_switch.rs`)

| Finding | Scenario test |
| --- | --- |
| round 3 HIGH invariants.rs:50: a dead role's event received at 100 s | `a_dead_role_hands_the_event_to_the_operator_by_the_bound_not_at_recovery` |
| round 3 HIGH time.rs:155: a fired wake freezes time while the session works | `a_woken_session_that_keeps_working_does_not_freeze_time_...` |
| round 3 HIGH transition.rs:177: reactivation with an overdue event | `activation_is_refused_with_an_urgent_event_...`, `reactivation_after_a_rollback_...` |
| round 3 MED params.rs:26: one budget for all classes | `each_urgent_class_has_its_own_budget_...` |
| round 4 HIGH time.rs:95: an alarm closed the event of a dead role | `a_dead_role_hands_the_event_...` (it stays pending) |
| round 4 HIGH invariants.rs:15: rollback and a lost GO released the bound | `rollback_and_a_lost_go_keep_the_deadline_...` |
| round 4 HIGH time.rs:126: a queued wake froze time | `a_delayed_or_lost_wake_does_not_stop_time_...` |
| round 5 HIGH invariants.rs:15: an alarm at 60 s masked a session receipt at 100 s | `a_woken_session_that_keeps_working_...`, `rollback_and_a_lost_go_...` (`misses`: `Unread`, then `Late` after the receipt) |
| round 7 HIGH invariants.rs:15: an alarm let a live session hold nothing at 100 s | `a_dead_role_hands_the_event_...` (recovery at 90 s), `a_hung_source_with_a_lost_fallback_wake_...`; `NoRecovery` shows `SessionBound` |
| round 7 HIGH transition.rs:77: no retry after rollback or a lost GO | `rollback_and_a_lost_go_keep_a_retry_until_the_session_has_read_the_event` |
| round 6 HIGH invariants.rs:21, time.rs:164: `Kill` cleared the session deadline and ended a working session | no `Kill`: the same scenarios assert the miss stays reported with the session alive or dead |
| round 5 HIGH time.rs:106: an armed wait that cannot fire blocked the nudge | `a_hung_source_with_a_lost_fallback_wake_still_gets_a_nudge_...` |
| state r2 C3/R1, alwayson r2: hung adapter, failed wake, five-minute supervisor | `an_unread_event_on_a_hung_source_...`, `a_failed_wake_is_recovered_...`, `a_hung_adapter_does_not_delay_...`, `a_five_minute_supervisor_...` |
| alwayson r3: activation, rollback, per-role GO | `activation_needs_shadow_fence_...`, `rollback_and_a_lost_go_...` |

Each guard a finding named is a `Defect` (`NoAlarm`, `NoRecovery`, `NoSupervisor`, `ActivateWithoutGates`,
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
