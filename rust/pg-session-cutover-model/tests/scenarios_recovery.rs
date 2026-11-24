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
        Stage::Received,
        "adoption delivers the waiting event with the card"
    );
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
fn a_stalled_launch_and_a_retry_hand_the_event_over_by_the_bound_and_deliver_it_at_adoption() {
    // gpt-6.2-sol-high rounds 2 and 3 HIGH: the bound runs on elapsed time across recovery.
    let mut p = params(vec![URGENT(0)], 0);
    p.sessions = 3;
    let mut m = armed_ready(p);
    run(&mut m, &[Event::Occur(0), Event::Scan(0), Event::Crash(0)]);
    // t=20 s: no live current session; the supervisor owes a launch within one pass.
    ticks(&mut m, SUPERVISOR_SECS);
    assert_eq!(m.apply(Event::Tick), Err(Denied::Deadline));
    run(&mut m, &[Event::BeginRecycle]);
    // t=50 s: the launch never confirms; time stops at the launch budget until it is aborted.
    ticks(&mut m, RECYCLE_SECS);
    assert_eq!(m.apply(Event::Tick), Err(Denied::Deadline));
    run(&mut m, &[Event::AbortRecycle]);
    // t=60 s: the sources are read again; the supervisor owes another launch.
    ticks(&mut m, 10);
    scan_all(&mut m);
    assert_eq!(
        m.apply(Event::Escalate(0)),
        Err(Denied::Guard),
        "not before the takeover age"
    );
    ticks(&mut m, 10);
    // t=70 s: nothing can read the event, so by the bound it is handed to today's path (the
    // alarm); without that, time stops before 90 s.
    run(&mut m, &[Event::BeginRecycle]);
    let mut silent = m.clone();
    ticks(&mut silent, 20);
    assert_eq!(silent.apply(Event::Tick), Err(Denied::Deadline));
    run(&mut m, &[Event::Escalate(0)]);
    // t=100 s: the second launch takes its whole budget, then adoption delivers the event.
    ticks(&mut m, RECYCLE_SECS);
    assert_eq!(
        m.age_secs(0),
        100,
        "the clock ran through the whole recovery"
    );
    assert_eq!((m.stage(0), m.escalated(0)), (Stage::Detected, true));
    run(&mut m, &[Event::Confirm, Event::Adopt]);
    assert_eq!(m.stage(0), Stage::Received);
    assert_eq!(m.violations(), vec![]);
}
