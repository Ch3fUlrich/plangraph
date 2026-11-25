//! Scenarios of delivery and recovery: lost wakes, a retry after switch-off, the authorized
//! recovery of an idle session, a working session that is never ended, binding deadlines.
mod common;
use common::*;
use pg_session_cutover_model::*;

#[test]
fn a_delayed_or_lost_wake_does_not_stop_time_and_the_fallback_keeps_the_original_deadline() {
    // time.rs:126 (round 4): a queued wake is not an obligation; delivery may be late or lost.
    let mut m = armed_idle(params(vec![go(0)]));
    run(
        &mut m,
        &[Event::Occur(0), Event::Scan(0), Event::WaiterFire],
    );
    assert!(m.notified() && m.life() == Life::Idle);
    ticks(&mut m, 10);
    run(&mut m, &[Event::WakeLost]);
    ticks(&mut m, 10);
    assert_eq!(
        m.apply(Event::Tick),
        Err(Denied::Deadline),
        "the supervisor owes a pass"
    );
    run(&mut m, &[Event::Nudge]);
    ticks(&mut m, 20);
    // t=40 s: the takeover wakes the session again, and that wake is lost too.
    run(&mut m, &[Event::Escalate(0), Event::WakeLost, Event::Nudge]);
    ticks(&mut m, 20);
    // t=60 s: still unread, so the operator holds it by the alarm age of the original deadline.
    assert_eq!(m.apply(Event::Tick), Err(Denied::Deadline));
    scan_all(&mut m);
    run(&mut m, &[Event::Alarm(0), Event::Nudge]);
    assert_eq!(
        (m.alarmed(0), m.operator_secs(0), m.age_secs(0)),
        (true, Some(60), 60)
    );
    // The last wake arrives late: the session begins its turn and receives.
    run(&mut m, &[Event::BeginTurn, Event::Receive]);
    assert_eq!((m.stage(0), m.session_secs(0)), (Stage::Received, Some(60)));
}

#[test]
fn a_hung_source_with_a_lost_fallback_wake_still_gets_a_nudge_and_ends_at_the_bound() {
    // time.rs:106 (round 5): an armed wait that cannot fire must not stop the supervisor.
    let mut m = armed_idle(params(vec![go(0)]));
    run(&mut m, &[Event::Hang(0), Event::Occur(0)]);
    ticks(&mut m, 40);
    // t=40 s: the armed wait cannot fire (nothing was read); the fallback wake is lost.
    run(&mut m, &[Event::Escalate(0), Event::WakeLost]);
    ticks(&mut m, 10);
    assert_eq!(
        m.apply(Event::Tick),
        Err(Denied::Deadline),
        "an armed wait is not attendance"
    );
    run(&mut m, &[Event::Nudge]);
    ticks(&mut m, 10);
    scan_all(&mut m);
    run(&mut m, &[Event::Alarm(0), Event::Nudge]);
    // One branch: the nudge is delivered and the session reads it at 60 s.
    let mut delivered = m.clone();
    run(&mut delivered, &[Event::BeginTurn, Event::Receive]);
    assert_eq!(
        (delivered.stage(0), delivered.session_secs(0)),
        (Stage::Received, Some(60))
    );
    // Another: every wake is lost. The retry stays open at every pass; at 90 s the session is
    // idle and has read nothing, so the authorized recovery is owed and its successor reads.
    run(&mut m, &[Event::WakeLost]);
    ticks(&mut m, 20);
    run(&mut m, &[Event::Nudge, Event::WakeLost]);
    ticks(&mut m, 10);
    assert_eq!(
        m.apply(Event::Tick),
        Err(Denied::Deadline),
        "recovery is due at the bound"
    );
    run(&mut m, &[Event::Recover]);
    assert_eq!(
        (m.life(), m.stage(0), m.session_secs(0)),
        (Life::Idle, Stage::Received, Some(90))
    );
    assert_eq!(
        (m.operator_secs(0), m.misses(), m.violations()),
        (Some(60), vec![], vec![])
    );
}

#[test]
fn rollback_and_a_lost_go_keep_a_retry_until_the_session_has_read_the_event() {
    // transition.rs:77 (round 7): each switch-off queues one wake; if it is lost, retry.
    for off in [Event::Rollback, Event::RevokeGo] {
        let mut m = armed_idle(params(vec![go(0)]));
        run(&mut m, &[Event::Occur(0), off, Event::WakeLost]);
        assert_eq!((m.mode(), m.life()), (Mode::Off, Life::Idle));
        ticks(&mut m, 30);
        run(&mut m, &[Event::Nudge, Event::WakeLost]);
        ticks(&mut m, 30);
        run(&mut m, &[Event::Nudge, Event::BeginTurn, Event::Receive]);
        assert_eq!(
            (m.stage(0), m.session_secs(0), m.misses()),
            (Stage::Received, Some(60), vec![])
        );
        // With the retry disabled the idle session is never woken again.
        let mut stuck = armed_idle(params(vec![go(0)])).with_defect(Defect::NoSupervisor);
        run(&mut stuck, &[Event::Occur(0), off, Event::WakeLost]);
        assert_eq!(stuck.apply(Event::Nudge), Err(Denied::Guard));
    }
}

#[test]
fn a_working_session_is_never_recovered_and_its_miss_is_reported() {
    // Round 6: a receipt timeout does not end a working session.
    let mut m = armed_idle(params(vec![go(0)]));
    run(
        &mut m,
        &[
            Event::Occur(0),
            Event::Scan(0),
            Event::WaiterFire,
            Event::BeginTurn,
        ],
    );
    pass(&mut m, 60);
    run(&mut m, &[Event::Escalate(0), Event::Alarm(0)]);
    assert_eq!(
        m.apply(Event::Recover),
        Err(Denied::Guard),
        "it is working: no recovery"
    );
    pass(&mut m, 40);
    assert_eq!(
        (m.life(), m.violations(), m.misses()),
        (Life::Active, vec![], vec![Miss::Unread(0)])
    );
    // Ending the turn after the bound does not make a recovery owed: the miss is already past.
    run(&mut m, &[Event::EndTurn]);
    assert_eq!(m.violations(), vec![]);
}

#[test]
fn rollback_and_a_lost_go_keep_the_deadline_of_an_event_that_occurred_while_active() {
    // invariants.rs:15 (round 4): a mode change must not release a binding event.
    for off in [Event::Rollback, Event::RevokeGo] {
        let mut m = armed_idle(params(vec![go(0)]));
        run(&mut m, &[Event::Occur(0), off, Event::BeginTurn]);
        assert_eq!(m.mode(), Mode::Off);
        // Off, with the session working unread: the deadline still runs and the alarm still comes.
        ticks(&mut m, 60);
        run(&mut m, &[Event::Alarm(0)]);
        assert_eq!((m.alarmed(0), m.operator_secs(0)), (true, Some(60)));
        ticks(&mut m, 20);
        run(&mut m, &[Event::Receive]);
        assert_eq!((m.stage(0), m.session_secs(0)), (Stage::Received, Some(80)));
        assert_eq!(m.misses(), vec![], "read at 80 s: inside the bound");
        // The operator is told but the session works on unread: the miss is reported, and a
        // late receipt keeps it visible.
        let mut slow = armed_idle(params(vec![go(0)]));
        run(&mut slow, &[Event::Occur(0), off, Event::BeginTurn]);
        ticks(&mut slow, 60);
        run(&mut slow, &[Event::Alarm(0)]);
        ticks(&mut slow, 40);
        assert_eq!(slow.violations(), vec![]);
        assert_eq!(slow.misses(), vec![Miss::Unread(0)]);
        run(&mut slow, &[Event::Receive]);
        assert_eq!(slow.misses(), vec![Miss::Late(0)]);
        // With no alarm either, the operator is never told.
        let mut late = armed_idle(params(vec![go(0)])).with_defect(Defect::NoAlarm);
        run(&mut late, &[Event::Occur(0), off, Event::BeginTurn]);
        ticks(&mut late, 100);
        assert_eq!(late.violations(), vec![Invariant::UrgentBound]);
    }
}
