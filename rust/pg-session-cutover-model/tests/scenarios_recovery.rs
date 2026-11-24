//! Scenarios of recovery: a dead or unarmed current session, a stalled launch, a slow recovery.
mod common;
use common::*;
use pg_session_cutover_model::*;

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
fn a_stalled_launch_is_bounded_and_a_slow_recovery_still_reads_the_urgent_event() {
    // gpt-6.2-sol-high round 2 HIGH: a fenced candidate must not count as attendance forever.
    let mut p = params(vec![URGENT(0)], 0);
    p.sessions = 3;
    let mut m = armed_ready(p);
    run(&mut m, &[Event::Occur(0), Event::Scan(0), Event::Crash(0)]);
    // No live current session: the supervisor owes a launch within one pass, event time is paused.
    ticks(&mut m, SUPERVISOR_SECS);
    assert_eq!(m.apply(Event::Tick), Err(Denied::Deadline));
    run(&mut m, &[Event::BeginRecycle]);
    // The launch never confirms: time stops at the launch budget until it is aborted.
    ticks(&mut m, RECYCLE_SECS);
    assert_eq!(m.apply(Event::Tick), Err(Denied::Deadline));
    run(&mut m, &[Event::AbortRecycle]);
    scan_all(&mut m);
    ticks(&mut m, SUPERVISOR_SECS);
    assert_eq!(m.apply(Event::Tick), Err(Denied::Deadline));
    assert_eq!(
        m.age_secs(0),
        0,
        "no session lived to read it, so its clock did not run"
    );
    run(&mut m, &[Event::BeginRecycle, Event::Confirm]);
    ticks(&mut m, 10);
    run(&mut m, &[Event::Adopt]);
    // The adopted session is handed the waiting event with its card and reads it at once.
    assert_eq!(m.apply(Event::Tick), Err(Denied::Deadline));
    run(&mut m, &[Event::Receive(2)]);
    assert!(m.stage(0) == Stage::Received && m.age_secs(0) <= URGENT_BOUND_SECS);
    assert_eq!(m.violations(), vec![]);
}
