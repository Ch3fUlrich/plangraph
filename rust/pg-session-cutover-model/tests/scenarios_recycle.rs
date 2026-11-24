//! Scenarios of the supervised recycle: quiescence, fence, adoption and message carry.
//! One named scenario per cutover finding (state-gpt-r1, r2, alwayson-gpt-r1 to r4) and canary fact.
mod common;
use common::*;
use pg_session_cutover_model::*;

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
    // Adoption revoked the predecessor's wait, and the successor has not armed its own.
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
fn a_lost_go_or_a_rollback_aborts_a_launch_that_is_not_adopted() {
    // gpt-6.2-sol-high round 1 HIGH: Confirm, RevokeGo, Adopt switched a role that was off.
    for (name, off, confirm) in [
        ("revoke after launch", Event::RevokeGo, false),
        ("revoke after confirm", Event::RevokeGo, true),
        ("rollback after confirm", Event::Rollback, true),
    ] {
        let mut m = armed_ready(params(vec![], 0));
        run(&mut m, &[Event::BeginRecycle]);
        if confirm {
            run(&mut m, &[Event::Confirm]);
        }
        run(&mut m, &[off]);
        assert_eq!(
            (m.mode(), m.recycle(), m.adopted()),
            (Mode::Off, Recycle::Idle, 0),
            "{name}"
        );
        assert_eq!(m.life(1), Life::Stopped, "{name}");
        assert_eq!(m.apply(Event::Adopt), Err(Denied::Guard), "{name}");
        assert_eq!(m.apply(Event::Confirm), Err(Denied::Guard), "{name}");
    }
    // An adopted recycle is only cleaned up: the stop needs no GO, and no new launch starts.
    let mut m = armed_ready(params(vec![], 0));
    recycle_to_adoption(&mut m);
    run(&mut m, &[Event::RevokeGo, Event::StopFail, Event::StopOk]);
    assert_eq!(
        (m.mode(), m.recycle(), m.life(0), m.adopted()),
        (Mode::Off, Recycle::Idle, Life::Stopped, 1)
    );
    assert_eq!(m.apply(Event::BeginRecycle), Err(Denied::Mode));
}

#[test]
fn adoption_revokes_the_predecessors_wait_so_a_failed_stop_leaves_one_armed_session() {
    // gpt-6.2-sol-high round 1 HIGH: Adopt, Arm(successor), StopFail with the old wait armed.
    let mut m = armed_ready(params(vec![], 0));
    assert_eq!(m.waiter(0), Waiter::Armed);
    recycle_to_adoption(&mut m);
    assert_eq!(
        m.waiter(0),
        Waiter::Dead,
        "the old wait is revoked with the adoption"
    );
    run(&mut m, &[Event::Arm(1), Event::StopFail, Event::StopFail]);
    assert_eq!(
        (m.waiter(0), m.waiter(1), m.life(0)),
        (Waiter::Dead, Waiter::Armed, Life::Idle)
    );
    assert_eq!(m.violations(), vec![]);
    // Without the fence the old wait stays armed beside the new one.
    let mut bad = armed_ready(params(vec![], 0)).with_defect(Defect::NoFence);
    recycle_to_adoption_unchecked(&mut bad);
    assert_eq!(bad.apply(Event::Arm(1)), Ok(Outcome::Applied));
    assert_eq!(
        bad.violations(),
        vec![Invariant::SingleCurrent, Invariant::TwoArmed]
    );
}
