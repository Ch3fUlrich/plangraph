//! One named scenario per cutover finding of the review rounds (state-gpt-r1, r2 and
//! alwayson-gpt-r1 to r4) and per canary fact. Each passes on the model; where a guard is the
//! point, the same trace on the model with that guard removed breaks the named invariant.
mod common;
use common::*;
use pg_session_cutover_model::*;

/// Activated role; session 0 has armed its own wait, written handoff and card, and is idle.
fn armed_ready(p: Params) -> Model {
    let mut m = model(p);
    activate(&mut m);
    run(
        &mut m,
        &[
            Event::Nudge,
            Event::BeginTurn(0),
            Event::Arm(0),
            Event::MarkReady(0),
            Event::EndTurn(0),
        ],
    );
    assert_eq!((m.life(0), m.waiter(0)), (Life::Idle, Waiter::Armed));
    m
}

fn scan_all(m: &mut Model) {
    run(m, &[Event::Scan(0), Event::Scan(1)]);
}

/// The supervised recycle up to adoption: ready marker and card, launch, confirm, adopt.
fn recycle_to_adoption(m: &mut Model) {
    run(m, &[Event::BeginRecycle, Event::Confirm, Event::Adopt]);
}

#[test]
fn canary_facts_are_the_constants_of_the_model() {
    assert_eq!(STOP_BLOCK_CAP, 9);
    assert_eq!(URGENT_BOUND_SECS, 90);
    const { assert!(TAKEOVER_SECS + SUPERVISOR_SECS <= URGENT_BOUND_SECS) };
}

#[test]
fn five_minute_supervisor_cannot_meet_the_bound() {
    // alwayson-gpt-r2 HIGH: a missed wake waited for the five-minute watchdog.
    let mut p = params(vec![], 0);
    p.supervisor_secs = 300;
    assert_eq!(Model::new(p).err(), Some(ParamError::SupervisorTooSlow));
    let mut p = params(vec![], 0);
    p.scan_secs = 65;
    assert_eq!(Model::new(p).err(), Some(ParamError::Tick));
}

#[test]
fn urgent_event_beside_a_hung_adapter_is_handed_to_todays_wake_path_inside_90s() {
    // state-gpt-r2 C3/R1, alwayson-gpt-r2 urgent wake, A3 req 1 and 8.
    let mut m = armed_ready(params(vec![URGENT(0)], 0));
    run(&mut m, &[Event::Hang(0), Event::Occur(0)]);
    ticks(&mut m, 60);
    scan_all(&mut m);
    assert!(m.blind(0), "the first failed read is visible at once");
    assert_eq!(m.stage(0), Stage::Occurred, "a hung source reads nothing");
    assert_eq!(
        m.apply(Event::Escalate(0)),
        Err(Denied::Guard),
        "not before the takeover age"
    );
    ticks(&mut m, 10);
    // Without a takeover the next ticks run into the bound and time stops.
    let mut waiting = m.clone();
    ticks(&mut waiting, 20);
    assert_eq!(waiting.apply(Event::Tick), Err(Denied::Deadline));
    run(
        &mut m,
        &[Event::Escalate(0), Event::BeginTurn(0), Event::Receive(0)],
    );
    assert_eq!(m.stage(0), Stage::Received);
    assert!(m.escalated(0) && m.age_secs(0) <= URGENT_BOUND_SECS);
    // The same trace with no takeover lets the age pass the bound.
    let mut slow = armed_ready(params(vec![URGENT(0)], 0)).with_defect(Defect::NoTakeover);
    run(&mut slow, &[Event::Hang(0), Event::Occur(0)]);
    ticks(&mut slow, 60);
    scan_all(&mut slow);
    ticks(&mut slow, 40);
    assert_eq!(slow.violations(), vec![Invariant::UrgentBound]);
}

#[test]
fn urgent_event_with_a_failed_wake_is_recovered_by_the_supervisor() {
    // alwayson-gpt-r2 HIGH: a failed wake waited for the five-minute supervisor.
    let mut m = armed_ready(params(vec![URGENT(0)], 0));
    run(
        &mut m,
        &[Event::Occur(0), Event::WaiterDie(0), Event::Scan(0)],
    );
    assert_eq!(
        m.apply(Event::WaiterFire(0)),
        Err(Denied::Guard),
        "the dead wait cannot wake"
    );
    assert_eq!(m.stage(0), Stage::Detected);
    ticks(&mut m, 20);
    assert_eq!(
        m.apply(Event::Tick),
        Err(Denied::Deadline),
        "one supervisor pass is due"
    );
    run(
        &mut m,
        &[Event::Nudge, Event::BeginTurn(0), Event::Receive(0)],
    );
    assert_eq!((m.stage(0), m.escalated(0)), (Stage::Received, false));
    assert!(m.age_secs(0) <= URGENT_BOUND_SECS);
}

#[test]
fn a_hung_adapter_does_not_delay_another_source() {
    // alwayson-gpt-r2 HIGH: slow sources are isolated; no read waits on another.
    let mut m = armed_ready(params(vec![URGENT(0), URGENT(1)], 0));
    run(
        &mut m,
        &[
            Event::Hang(0),
            Event::Occur(1),
            Event::Scan(0),
            Event::Scan(1),
        ],
    );
    assert!(m.blind(0) && !m.blind(1));
    run(
        &mut m,
        &[Event::WaiterFire(0), Event::BeginTurn(0), Event::Receive(0)],
    );
    assert_eq!(
        (m.stage(1), m.escalated(1), m.age_secs(1)),
        (Stage::Received, false, 0)
    );
}

#[test]
fn ordinary_events_wait_for_the_window_and_a_quiet_start_does_not_spin() {
    // state-gpt-r2 W1-W2: an ordinary event must not wake the wait early.
    let mut p = params(
        vec![
            ORDINARY(0),
            EventSpec {
                class: Class::Beat,
                source: 0,
            },
            EventSpec {
                class: Class::FollowUp,
                source: 1,
            },
        ],
        0,
    );
    p.stop_cap = 2;
    let mut m = armed_ready(p);
    assert_eq!(
        m.apply(Event::WaiterFire(0)),
        Err(Denied::Guard),
        "a quiet start has nothing to fire"
    );
    run(
        &mut m,
        &[
            Event::Occur(0),
            Event::Occur(1),
            Event::Occur(2),
            Event::Scan(0),
            Event::Scan(1),
        ],
    );
    assert_eq!(m.apply(Event::WaiterFire(0)), Err(Denied::Guard));
    run(
        &mut m,
        &[Event::WindowEnd(0), Event::BeginTurn(0), Event::Receive(0)],
    );
    assert!(
        (0..3).all(|e| m.stage(e) == Stage::Received),
        "heartbeat, beat and follow-up are delivered, never dropped"
    );
    // A window with nothing in it still wakes once and is received without effect.
    run(
        &mut m,
        &[
            Event::Arm(0),
            Event::EndTurn(0),
            Event::WindowEnd(0),
            Event::BeginTurn(0),
            Event::Receive(0),
        ],
    );
}

#[test]
fn a_pending_wake_never_recycles_a_working_session() {
    // alwayson-gpt-r2 HIGH, A3 req 2 and 8: a wake pending for a working session.
    let mut m = armed_ready(params(vec![URGENT(0)], 0));
    run(
        &mut m,
        &[
            Event::Occur(0),
            Event::Scan(0),
            Event::WaiterFire(0),
            Event::BeginTurn(0),
        ],
    );
    assert_eq!(
        m.stage(0),
        Stage::Detected,
        "woken, working, receipt still missing"
    );
    assert_eq!(m.apply(Event::BeginRecycle), Err(Denied::Active));
    assert_eq!(m.apply(Event::MarkReady(0)), Ok(Outcome::Applied));
    assert_eq!(
        m.apply(Event::BeginRecycle),
        Err(Denied::Active),
        "a card written mid-turn is not quiescence"
    );
    assert_eq!(
        m.apply(Event::EndTurn(0)),
        Ok(Outcome::Blocked),
        "the Stop hook holds the turn for the receipt"
    );
    // The model without the guard recycles the working session.
    let mut bad = armed_ready(params(vec![URGENT(0)], 0)).with_defect(Defect::RecycleWhileActive);
    run(
        &mut bad,
        &[
            Event::Occur(0),
            Event::Scan(0),
            Event::WaiterFire(0),
            Event::BeginTurn(0),
        ],
    );
    run(&mut bad, &[Event::BeginRecycle, Event::Confirm]);
    assert_eq!(bad.apply(Event::Adopt), Ok(Outcome::Applied));
    assert_eq!(bad.violations(), vec![Invariant::AdoptedOverActive]);
}

#[test]
fn a_missing_receipt_during_an_active_turn_never_recycles_and_a_stale_card_does_not_either() {
    // alwayson-gpt-r2 HIGH, A3 req 2: no recycle on a missing receipt; the card must be fresh.
    let mut p = params(vec![URGENT(0)], 0);
    p.sessions = 3;
    let mut m = armed_ready(p);
    run(
        &mut m,
        &[Event::Occur(0), Event::Scan(0), Event::WaiterFire(0)],
    );
    // Woken but not yet in a turn: a recycle now would end a session that is about to work.
    assert_eq!(
        m.apply(Event::BeginRecycle),
        Ok(Outcome::Applied),
        "idle with a fresh card is quiescent"
    );
    assert_eq!(m.apply(Event::AbortRecycle), Ok(Outcome::Applied));
    run(&mut m, &[Event::BeginTurn(0)]);
    assert_eq!(m.apply(Event::BeginRecycle), Err(Denied::Active));
    run(
        &mut m,
        &[Event::Receive(0), Event::Arm(0), Event::EndTurn(0)],
    );
    // The new turn made the old card stale: the next recycle needs a new ready marker.
    assert_eq!(m.apply(Event::BeginRecycle), Err(Denied::Guard));
    run(
        &mut m,
        &[
            Event::WindowEnd(0),
            Event::BeginTurn(0),
            Event::Arm(0),
            Event::MarkReady(0),
            Event::EndTurn(0),
            Event::BeginRecycle,
        ],
    );
}

#[test]
fn a_failed_predecessor_stop_leaves_one_current_session_and_a_superseded_one_may_end() {
    // state-gpt-r1 S7, alwayson-gpt-r2 HIGH, alwayson-gpt-r3 HIGH, state-gpt-r2 H3-H4.
    let mut m = armed_ready(params(vec![], 1));
    run(
        &mut m,
        &[Event::BeginRecycle, Event::Confirm, Event::WindowEnd(0)],
    );
    run(&mut m, &[Event::Adopt]);
    assert!(m.fenced(0) && !m.fenced(1) && m.adopted() == 1);
    assert_eq!(m.apply(Event::StopFail), Ok(Outcome::StopFailed));
    assert_eq!(
        (m.life(0), m.live_current()),
        (Life::Idle, 1),
        "the old session lives, one is current"
    );
    assert_eq!(
        m.apply(Event::BeginRecycle),
        Err(Denied::Mode),
        "no new recycle while a stop is pending"
    );
    // The fenced session wakes, may not act, and may end although it has no wait or marker.
    run(&mut m, &[Event::BeginTurn(0)]);
    assert_eq!(m.apply(Event::Arm(0)), Err(Denied::Fence));
    assert_eq!(m.apply(Event::Receive(0)), Err(Denied::Fence));
    assert_eq!(m.apply(Event::MarkReady(0)), Err(Denied::Fence));
    assert_eq!(m.apply(Event::EndTurn(0)), Ok(Outcome::Applied));
    run(&mut m, &[Event::StopFail, Event::StopOk]);
    assert_eq!((m.life(0), m.recycle()), (Life::Stopped, Recycle::Idle));
    // Without the fence two sessions are current.
    let mut bad = armed_ready(params(vec![], 0)).with_defect(Defect::NoFence);
    run(&mut bad, &[Event::BeginRecycle, Event::Confirm]);
    assert_eq!(bad.apply(Event::Adopt), Ok(Outcome::Applied));
    assert_eq!(bad.violations(), vec![Invariant::SingleCurrent]);
}

#[test]
fn a_wait_that_dies_after_the_stop_hook_accepted_it_is_recovered() {
    // alwayson-gpt-r1 HIGH (Stop and recovery): a live session with a dead waiter.
    let mut m = armed_ready(params(vec![URGENT(0)], 0));
    run(
        &mut m,
        &[Event::WaiterDie(0), Event::Occur(0), Event::Scan(0)],
    );
    ticks(&mut m, 20);
    assert_eq!(m.apply(Event::Tick), Err(Denied::Deadline));
    run(
        &mut m,
        &[Event::Nudge, Event::BeginTurn(0), Event::Receive(0)],
    );
    assert_eq!(
        m.apply(Event::EndTurn(0)),
        Ok(Outcome::Blocked),
        "the hook asks for a new wait"
    );
    run(&mut m, &[Event::Arm(0), Event::EndTurn(0)]);
    // Taking the Stop hook for proof (no supervisor) leaves the session asleep.
    let mut bad = armed_ready(params(vec![], 0)).with_defect(Defect::NoSupervisor);
    run(&mut bad, &[Event::WaiterDie(0)]);
    ticks(&mut bad, 30);
    assert_eq!(bad.violations(), vec![Invariant::Unattended]);
}

#[test]
fn the_stop_hook_is_a_help_not_a_proof() {
    // alwayson-gpt-r1 HIGH and canary section 5: nine blocks, then the turn ends unarmed.
    let mut m = model(params(vec![], 0));
    activate(&mut m);
    run(&mut m, &[Event::Nudge, Event::BeginTurn(0)]);
    for n in 0..STOP_BLOCK_CAP {
        assert_eq!(
            m.apply(Event::EndTurn(0)),
            Ok(Outcome::Blocked),
            "block {}",
            n + 1
        );
    }
    assert_eq!(m.apply(Event::EndTurn(0)), Ok(Outcome::Applied));
    assert_eq!((m.life(0), m.waiter(0)), (Life::Idle, Waiter::None));
    ticks(&mut m, 20);
    assert_eq!(
        m.apply(Event::Tick),
        Err(Denied::Deadline),
        "so the supervisor owns the recovery"
    );
    run(
        &mut m,
        &[
            Event::Nudge,
            Event::BeginTurn(0),
            Event::Arm(0),
            Event::EndTurn(0),
        ],
    );
}

#[test]
fn a_successor_that_fails_to_arm_is_nudged_and_the_predecessors_wait_is_not_carried() {
    // alwayson-gpt-r1 HIGH, A3 req 8, canary section 4: only the successor's own wait counts.
    let mut m = armed_ready(params(vec![URGENT(0), URGENT(0)], 0));
    run(
        &mut m,
        &[
            Event::BeginRecycle,
            Event::Confirm,
            Event::EndTurn(1),
            Event::Adopt,
        ],
    );
    assert_eq!(
        m.waiter(0),
        Waiter::Armed,
        "the predecessor still holds its wait"
    );
    run(&mut m, &[Event::FencedWaiterExit(0)]);
    assert_eq!((m.waiter(0), m.waiter(1)), (Waiter::Dead, Waiter::None));
    // An event now has no armed wait of the current generation: it stays pending, not lost.
    run(&mut m, &[Event::Occur(0), Event::Scan(0)]);
    assert_eq!(m.stage(0), Stage::Detected);
    assert_eq!(m.apply(Event::WaiterFire(0)), Err(Denied::Guard));
    assert_eq!(m.apply(Event::WaiterFire(1)), Err(Denied::Guard));
    ticks(&mut m, 20);
    assert_eq!(m.apply(Event::Tick), Err(Denied::Deadline));
    run(
        &mut m,
        &[
            Event::Nudge,
            Event::BeginTurn(1),
            Event::Receive(1),
            Event::Arm(1),
            Event::EndTurn(1),
        ],
    );
    assert_eq!(m.stage(0), Stage::Received);
    run(
        &mut m,
        &[
            Event::StopOk,
            Event::Occur(1),
            Event::Scan(0),
            Event::WaiterFire(1),
        ],
    );
}

#[test]
fn a_delivered_go_followed_by_unrelated_tool_use_is_still_carried_at_recycle() {
    // alwayson-gpt-r2 HIGH (direct-message carry), A3 req 3 and 8.
    let mut m = armed_ready(params(vec![], 1));
    run(
        &mut m,
        &[
            Event::SendMsg(0),
            Event::BeginTurn(0),
            Event::ReadMsg { msg: 0, session: 0 },
            Event::ToolUse(0),
            Event::MarkReady(0),
            Event::EndTurn(0),
        ],
    );
    assert!(!m.handled(0), "a later tool call is not a handling record");
    recycle_to_adoption(&mut m);
    assert_eq!(m.home(0), Home::Session(1));
    run(
        &mut m,
        &[
            Event::StopOk,
            Event::ReadMsg { msg: 0, session: 1 },
            Event::HandleMsg { msg: 0, session: 1 },
        ],
    );
    assert!(m.handled(0));
    // Counting the tool call as handling loses the message.
    let mut bad = armed_ready(params(vec![], 1)).with_defect(Defect::LaterToolCallHandles);
    run(
        &mut bad,
        &[
            Event::SendMsg(0),
            Event::BeginTurn(0),
            Event::ReadMsg { msg: 0, session: 0 },
        ],
    );
    assert_eq!(bad.apply(Event::ToolUse(0)), Ok(Outcome::Applied));
    assert_eq!(bad.violations(), vec![Invariant::HandledWithoutAction]);
}

#[test]
fn a_message_that_reaches_the_predecessor_after_the_forward_snapshot_is_carried_at_adoption() {
    // canary section 4 (only unread router messages are forwarded), alwayson-gpt-r1 HIGH.
    let mut m = armed_ready(params(vec![], 2));
    run(
        &mut m,
        &[
            Event::SendMsg(0),
            Event::BeginRecycle,
            Event::SendMsg(1),
            Event::Confirm,
        ],
    );
    assert_eq!((m.home(0), m.home(1)), (Home::Session(0), Home::Session(0)));
    run(&mut m, &[Event::Adopt, Event::StopOk]);
    assert_eq!((m.home(0), m.home(1)), (Home::Session(1), Home::Session(1)));
    assert_eq!(m.violations(), vec![]);
    // A forward limited to the snapshot strands the later message on the stopped session.
    let mut bad = armed_ready(params(vec![], 2)).with_defect(Defect::CarrySnapshotOnly);
    run(
        &mut bad,
        &[
            Event::SendMsg(0),
            Event::BeginRecycle,
            Event::SendMsg(1),
            Event::Confirm,
            Event::Adopt,
        ],
    );
    assert_eq!(bad.apply(Event::StopOk), Ok(Outcome::Applied));
    assert_eq!(bad.violations(), vec![Invariant::MessageStranded]);
}

#[test]
fn an_unread_go_from_before_the_card_stamp_is_not_hidden_by_the_card() {
    // alwayson-gpt-r1 HIGH (automatic handoff): the carry follows records, not the card time.
    let mut m = armed_ready(params(vec![], 1));
    run(
        &mut m,
        &[
            Event::SendMsg(0),
            Event::BeginTurn(0),
            Event::MarkReady(0),
            Event::EndTurn(0),
        ],
    );
    recycle_to_adoption(&mut m);
    assert_eq!(m.home(0), Home::Session(1));
    run(
        &mut m,
        &[
            Event::StopOk,
            Event::ReadMsg { msg: 0, session: 1 },
            Event::HandleMsg { msg: 0, session: 1 },
        ],
    );
}

#[test]
fn a_crashed_current_session_is_recovered_and_its_messages_are_replayed() {
    // state-gpt-r2 H3-H4: a live-looking role with nothing behind it; the inbox is durable.
    let mut m = armed_ready(params(vec![URGENT(0)], 2));
    run(
        &mut m,
        &[
            Event::SendMsg(0),
            Event::Occur(0),
            Event::Scan(0),
            Event::Crash(0),
            Event::SendMsg(1),
        ],
    );
    assert_eq!(
        m.home(1),
        Home::Inbox,
        "no live session: the durable inbox holds it"
    );
    ticks(&mut m, 20);
    assert_eq!(
        m.apply(Event::Tick),
        Err(Denied::Deadline),
        "recovery is due"
    );
    run(&mut m, &[Event::BeginRecycle, Event::Confirm, Event::Adopt]);
    assert_eq!(
        (m.home(0), m.home(1), m.recycle()),
        (Home::Session(1), Home::Session(1), Recycle::Idle)
    );
    assert_eq!(
        m.stage(0),
        Stage::Detected,
        "the event waited in the digest"
    );
    run(&mut m, &[Event::Receive(1)]);
    assert_eq!(m.stage(0), Stage::Received);
}

#[test]
fn a_session_without_a_fresh_card_cannot_be_replaced_only_woken() {
    // state-gpt-r2 H4: the supervised path validates the handoff before it replaces a live session.
    let mut m = model(params(vec![], 0));
    activate(&mut m);
    assert_eq!(m.apply(Event::BeginRecycle), Err(Denied::Guard));
    run(&mut m, &[Event::Nudge]);
}

#[test]
fn activation_needs_shadow_fence_recovery_go_and_no_session_cron_and_rollback_restores_todays_path()
{
    // A3 req 5, 7 and 7a: the design switches nothing by itself.
    let mut m = model(params(vec![URGENT(0)], 0));
    assert_eq!(m.apply(Event::Activate), Err(Denied::Mode));
    run(&mut m, &[Event::ArmCron(0), Event::StartShadow]);
    for gate in [
        Event::SignOffShadow,
        Event::DeployFence(Fence::QuiescentHandoff),
        Event::DeployRecovery,
    ] {
        assert_eq!(m.apply(Event::Activate), Err(Denied::Gate));
        run(&mut m, &[gate]);
    }
    assert_eq!(
        m.apply(Event::Activate),
        Err(Denied::Gate),
        "no live GO for the role"
    );
    run(&mut m, &[Event::GrantGo]);
    assert_eq!(
        m.apply(Event::Activate),
        Err(Denied::Cron),
        "follow-ups replace the session cron first"
    );
    run(&mut m, &[Event::ClearCron(0), Event::Activate]);
    assert_eq!(m.apply(Event::ArmCron(0)), Err(Denied::Cron));
    // A verified quiescent handoff activates the role but allows no automatic recycle.
    assert_eq!(m.apply(Event::BeginRecycle), Err(Denied::Gate));
    // Rollback: the unreceived event goes to today's path and the Stop hook stops holding.
    run(&mut m, &[Event::Occur(0), Event::Scan(0), Event::Rollback]);
    assert_eq!(
        (m.mode(), m.escalated(0), m.notified(0)),
        (Mode::Off, true, true)
    );
    run(&mut m, &[Event::BeginTurn(0)]);
    assert_eq!(m.apply(Event::Arm(0)), Err(Denied::Mode));
    run(&mut m, &[Event::Receive(0)]);
    assert_eq!(
        (m.stage(0), m.apply(Event::EndTurn(0))),
        (Stage::Received, Ok(Outcome::Applied))
    );
    // Without the gates the role would be active unfenced.
    let mut bad = model(params(vec![], 0)).with_defect(Defect::ActivateWithoutGates);
    run(&mut bad, &[Event::StartShadow]);
    assert_eq!(bad.apply(Event::Activate), Ok(Outcome::Applied));
    assert_eq!(bad.violations(), vec![Invariant::ActivationGate]);
}

#[test]
fn losing_the_go_switches_the_role_back_and_an_unswitched_role_keeps_todays_wake() {
    let mut m = model(params(vec![URGENT(0)], 0));
    // Before the switch every urgent event is already on today's path: nothing waits on a digest.
    run(&mut m, &[Event::Occur(0)]);
    assert!(m.escalated(0) && m.notified(0));
    let mut m = model(params(vec![], 0));
    activate(&mut m);
    run(&mut m, &[Event::RevokeGo]);
    assert_eq!(m.mode(), Mode::Off);
    assert_eq!(
        m.apply(Event::Activate),
        Err(Denied::Mode),
        "a new shadow sign-off and GO are needed"
    );
}
