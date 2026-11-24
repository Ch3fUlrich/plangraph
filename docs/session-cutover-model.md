# Session cutover model

`rust/pg-session-cutover-model` is the executable design of step A3 of the always-on plan
(SESSION-STATE-CUTOVER-DESIGN): one orchestrator role moving from today's waits to the digest
wait of `plangraph state wait` ([state-shadow.md](state-shadow.md)). It is a pure Rust model
beside [pg-launch-recovery-model](../rust/pg-launch-recovery-model/README.md) and
[pg-session-admission-model](../rust/pg-session-admission-model/README.md): no I/O, no async, no
clock, no new dependency. **It switches nothing live**: no hook, unit, watchdog or launcher
changes, and no wait or wake path is removed until a reviewed design is accepted and a role has
its GO (rule 5.9). One instance models one role; the two orchestrator roles are two instances.

## Actors and state

Session (0 to 2; idle or in a turn; fenced or current; ready marker; cron), waiter (none,
armed, fired, dead; bound to its session), queued wake (`notify`), event (future, occurred,
detected in a digest, received; age; escalated), source (hung or not; last read), supervisor,
recycle (idle, launching, confirmed, stop pending), direct message (home, read, effect done,
handling record), role switch (off, shadow, active) with its gates. `Model::apply` is one atomic
transition; a refused event leaves the model unchanged. `Tick` is 10 s and is refused while an
obligation is due, so a scan, wake, takeover or supervisor pass cannot be skipped by waiting.

| Parameter | Value | Source |
| --- | --- | --- |
| urgent bound | 90 s | rule 3.48, D-6578 |
| takeover of today's wake path | 70 s | parameter, from shadow latencies |
| supervisor pass | at most 20 s | parameter; `takeover + pass <= bound` is checked |
| one read per source | at most 60 s | A2 scan period; a hung read is visible at once |
| Stop-hook cap | 9 | canary-a1, Claude Code 2.1.329 |

## Invariants (`Model::violations`)

| Invariant | Requirement of plan section 4 A3 |
| --- | --- |
| `UrgentBound`: an unreceived urgent event older than 90 s is already on today's wake path | 1, 4 |
| `Unattended`: an active role is never idle with nothing armed or woken for more than one supervisor pass | 4 |
| `SingleCurrent`: at most one live session is current; the adopted one is never fenced | 2 |
| `AdoptedOverActive`: no recycle takes over a session that is working or whose card is stale | 2 |
| `RecycleShape`: one recycle at a time; the successor waits fenced until adoption | 2 |
| `MessageStranded`: no unhandled message sits on a stopped, fenced session | 3 |
| `HandledWithoutAction`: a handling record exists only after the effect | 3 |
| `CronInActive`: no session cron in an active role | 5 |
| `ActivationGate`: active needs shadow sign-off, a fence, deployed recovery and a live GO | 7, 7a |
| `StopCap`: the Stop hook never holds a turn past its cap | 4 |

Requirement 6 (digest size, scan cursor apart from the ack) and the quiet start are digest
format, not protocol: they are build slices. Requirement 7 is the `Rollback` and `RevokeGo`
transitions: both hand every unreceived event to today's path and end the Stop hook.

## Transitions

| Event | Guard | Effect |
| --- | --- | --- |
| `Occur` | event not yet occurred | occurred; handed to today's wake path at once unless the role is active |
| `Scan(s)` | role shadow or active | healthy: occurred events of `s` become detected; hung: `blind(s)` at once; never reads another source |
| `WaiterFire` | current session, wait armed, an urgent event detected | wait fired, session woken |
| `WindowEnd` | current session, wait armed | wait fired for ordinary, beat and follow-up events; also when quiet |
| `WaiterDie`, `FencedWaiterExit` | wait armed; fenced session | wait dead, no wake |
| `Arm` | in a turn, current generation, role active | own wait armed |
| `Receive` | in a turn, current generation | detected or escalated events received |
| `EndTurn` | in a turn | current session in an active role: blocked up to the cap until armed and received; fenced session never blocked |
| `Escalate` | active, urgent, unreceived, age at least 70 s | handed to today's wake path, current session woken |
| `Nudge` | active, current session idle and unarmed | session woken so it can re-arm |
| `BeginRecycle` | active, fence deployed, recovery deployed, current session idle with a fresh card or stopped | successor launched, fenced; forward snapshot taken |
| `Confirm`, `AbortRecycle` | launch live | confirmed; or successor stopped and recycle idle |
| `Adopt` | confirmed, predecessor quiescent | one compare-and-set: successor current, predecessor fenced, every message without a record carried and unread |
| `StopOk`, `StopFail` | stop pending | predecessor stopped; or retried (the supervisor always has this move) |
| `SendMsg`, `ReadMsg`, `HandleMsg` | current session in a turn for the last two | message to the current session or the durable inbox; read; effect and record in one step |
| `ArmCron` | session live, role not active | session cron (refused when active) |
| `StartShadow`, `SignOffShadow`, `DeployFence`, `DeployRecovery`, `GrantGo`, `Activate`, `Rollback`, `RevokeGo` | `Activate` needs shadow, sign-off, a fence, recovery, GO, no cron | role switch; `Rollback` and `RevokeGo` return to today's behaviour |

A quiescent handoff stands in for the fence for activation only: automatic recycle needs the
deployed fence. Events that have occurred stay pending through a recycle; the successor arms
its own wait (see below) and its first read finds them.

## The recycle fact built in

ctx-watchdog confirms the replacement live, then `claude stop`s the predecessor. It does not
wait for, carry or re-arm the predecessor's background wait, and `role_relaunch.forward`
carries only unread messages (canary-a1 section 4). So: the successor arms its own wait after
adoption (`Arm` is refused before); a fenced wait wakes nothing; a message that reaches the
predecessor after the forward snapshot is carried by `Adopt`, which follows records, not a time.

## Findings as scenarios (`tests/scenarios.rs`)

| Finding | Scenario test |
| --- | --- |
| state r2 C3/R1, alwayson r2: no end-to-end bound, hung adapter, failed wake | `urgent_event_beside_a_hung_adapter_...`, `urgent_event_with_a_failed_wake_...`, `a_hung_adapter_does_not_delay_another_source`, `five_minute_supervisor_cannot_meet_the_bound` |
| alwayson r2: a pending wake recycles a working session; missing receipt | `a_pending_wake_never_recycles_a_working_session`, `a_missing_receipt_during_an_active_turn_...` |
| state r1 S7, alwayson r2, r3: two armed sessions after a failed stop | `a_failed_predecessor_stop_leaves_one_current_session_...` |
| alwayson r2: a later tool call counted as handling | `a_delivered_go_followed_by_unrelated_tool_use_...` |
| alwayson r1: unread message before the card stamp or after the forward snapshot | `an_unread_go_from_before_the_card_stamp_...`, `a_message_that_reaches_the_predecessor_after_...` |
| alwayson r1: waiter dies after Stop accepted, successor fails to arm, cap | `a_wait_that_dies_after_the_stop_hook_...`, `a_successor_that_fails_to_arm_...`, `the_stop_hook_is_a_help_not_a_proof` |
| state r2 H3-H4: superseded session cannot end; live unarmed session; dead session | the fenced-session checks of the failed-stop test, `a_session_without_a_fresh_card_...`, `a_crashed_current_session_...` |
| state r2 W1-W2: ordinary event spins the wait; quiet start | `ordinary_events_wait_for_the_window_...` |
| alwayson r3: activation, rollback, per-role GO; req 5 crons and beats | `activation_needs_shadow_fence_...`, `losing_the_go_...`; beats and follow-ups in the window test |

Each guard a finding named is a `Defect`; `each_defect_is_caught_by_its_invariant` shows the
unsafe model breaks the listed invariant while the same search on the safe model finds nothing.

## Exhaustive bounds (`tests/exhaustive.rs`)

Production times; Stop-hook cap 2 (1 in the two-recycle search, 9 in the named scenario). Every ordering up to: wake, 1
session, 2 urgent events on 2 sources and 1 ordinary, 12 events; recycle, 2 sessions, 1 urgent
event, 2 messages, 11 events; recycles, 3 sessions (two recycles), 1 message, crashes, 16
events; gates, all mode and gate events with 2 sessions and 1 event, 15 events. Each checks that
refusals change nothing, invariants hold, event stages and handling records never go back, and
time is never blocked with no way out. Exploration found a real gap: a crash of the successor
while the predecessor's stop is pending left the supervisor no move; retrying the stop is one.

## Not modelled (owned elsewhere, see the slices)

Source adapters and event retention (A2), the shared journal and its authorization (B), keeper
generation issue (stage C), external effect receipts (D), digest size. The model assumes the
handling effect and its record are one step, effects are idempotent on replay, and a stop that
keeps failing, or all slots used, is an operator alarm. The time budget per urgent class is a
parameter set from the A2 shadow latencies; 90 s is the ruling of rule 3.48 for all classes.
