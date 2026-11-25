//! Scenarios of the urgent-event timing: one per finding of the review rounds that concerns the
//! 90 s bound (rule 3.48, D-6578), and per canary fact. Where a guard is the point, the same
//! trace on the model with that guard removed breaks the named invariant.
mod common;
use common::*;
use pg_session_cutover_model::*;

#[test]
fn the_ruling_and_the_default_budgets_are_consistent() {
    assert_eq!(URGENT_BOUND_SECS, 90);
    assert_eq!(params(vec![]).check(), Ok(()));
    for class in UrgentClass::ALL {
        let b = params(vec![]).budget(class);
        assert!(b.takeover_secs + SUPERVISOR_SECS <= b.alarm_secs);
        assert!(
            b.alarm_secs + SUPERVISOR_SECS <= b.bound_secs && b.bound_secs <= URGENT_BOUND_SECS
        );
    }
}

#[test]
fn a_five_minute_supervisor_cannot_meet_the_bound() {
    // alwayson-gpt-r2 HIGH: a missed wake waited for the five-minute watchdog.
    let mut p = params(vec![]);
    p.supervisor_secs = 300;
    assert_eq!(Model::new(p).err(), Some(ParamError::SupervisorTooSlow));
    let mut p = params(vec![]);
    p.scan_secs = 65;
    assert_eq!(Model::new(p).err(), Some(ParamError::Tick));
    let mut p = params(vec![]);
    p.scan_secs = 2550;
    assert_eq!(Model::new(p).err(), Some(ParamError::Range));
}

#[test]
fn each_urgent_class_has_its_own_budget_and_none_may_exceed_the_ruling() {
    // params.rs:26: A3 requirement 1 sets a time budget per urgent class.
    let mut p = params(vec![
        event(UrgentClass::GoRow, 0),
        event(UrgentClass::LeaseMargin, 0),
    ]);
    p.budgets[UrgentClass::GoRow as usize] = Budget {
        takeover_secs: 20,
        alarm_secs: 40,
        bound_secs: 60,
    };
    assert_eq!(p.check(), Ok(()));
    let mut m = armed_idle(p.clone());
    run(&mut m, &[Event::Occur(0), Event::Occur(1)]);
    assert_eq!(
        m.apply(Event::Escalate(0)),
        Err(Denied::Guard),
        "not before its takeover age"
    );
    pass(&mut m, 20);
    assert_eq!(
        m.apply(Event::Escalate(1)),
        Err(Denied::Guard),
        "the lease class waits longer"
    );
    run(&mut m, &[Event::Escalate(0), Event::BeginTurn]);
    pass(&mut m, 20);
    assert_eq!(m.apply(Event::Alarm(1)), Err(Denied::Guard));
    run(&mut m, &[Event::Alarm(0)]);
    assert_eq!(
        (m.alarmed(0), m.operator_secs(0)),
        (true, Some(40)),
        "go row held by the operator at its alarm age"
    );
    pass(&mut m, 20);
    run(&mut m, &[Event::Alarm(1)]);
    assert_eq!(m.age_secs(1), 60);
    // A class may not be given more than the ruling, nor a step that leaves no supervisor pass.
    let mut over = p.clone();
    over.budgets[UrgentClass::MainRed as usize].bound_secs = 100;
    assert_eq!(over.check(), Err(ParamError::OverRuling));
    let mut tight = p;
    tight.budgets[UrgentClass::RunFailed as usize] = Budget {
        takeover_secs: 50,
        alarm_secs: 60,
        bound_secs: 90,
    };
    assert_eq!(tight.check(), Err(ParamError::SupervisorTooSlow));
}

#[test]
fn an_unread_event_on_a_hung_source_reaches_a_reader_by_the_bound() {
    // state-gpt-r2 C3/R1, alwayson-gpt-r2: a hung adapter and the first failed read.
    let mut m = armed_idle(params(vec![go(0)]));
    run(&mut m, &[Event::Hang(0), Event::Occur(0)]);
    pass(&mut m, 30);
    assert_eq!(
        m.apply(Event::Escalate(0)),
        Err(Denied::Guard),
        "not before the takeover age"
    );
    pass(&mut m, 10);
    run(
        &mut m,
        &[Event::Escalate(0), Event::BeginTurn, Event::Receive],
    );
    assert_eq!(m.stage(0), Stage::Received);
    assert!(m.age_secs(0) <= URGENT_BOUND_SECS);
    run(&mut m, &[Event::Scan(0)]);
    assert!(m.blind(0), "the failed read is visible at once");
}

#[test]
fn a_woken_session_that_keeps_working_does_not_freeze_time_and_the_alarm_holds_the_bound() {
    // time.rs:155: a fired wake must not stop elapsed time while the session works unread.
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
    assert_eq!(m.stage(0), Stage::Detected);
    pass(&mut m, 40);
    assert_eq!(
        m.age_secs(0),
        40,
        "elapsed time ran while the session worked"
    );
    run(&mut m, &[Event::Escalate(0)]);
    pass(&mut m, 20);
    // At 60 s the session has still not read it: the operator is told. That is tracked apart
    // from the session's receipt, and does not stand in for it.
    run(&mut m, &[Event::Alarm(0)]);
    assert_eq!((m.alarmed(0), m.operator_secs(0)), (true, Some(60)));
    assert_eq!((m.stage(0), m.session_secs(0)), (Stage::Detected, None));
    let mut ignoring = m.clone();
    pass(&mut m, 20);
    run(&mut m, &[Event::Receive]);
    assert_eq!(
        (m.stage(0), m.session_secs(0), m.operator_secs(0)),
        (Stage::Received, Some(80), Some(60))
    );
    assert_eq!(m.misses(), vec![], "read inside the bound: no miss");
    // A session that ignores it keeps working: nothing ends it, time runs past the bound, and
    // the miss is reported although the operator was told. Reading late keeps the evidence.
    pass(&mut ignoring, 40);
    assert_eq!((ignoring.life(), ignoring.age_secs(0)), (Life::Active, 100));
    assert_eq!(ignoring.violations(), vec![]);
    assert_eq!(ignoring.misses(), vec![Miss::Unread(0)]);
    run(&mut ignoring, &[Event::Receive]);
    assert_eq!(ignoring.misses(), vec![Miss::Late(0)]);
    // A session that reads in time closes the event itself and the alarm has nothing to do.
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
    pass(&mut m, 20);
    run(&mut m, &[Event::Receive]);
    assert_eq!((m.stage(0), m.alarmed(0)), (Stage::Received, false));
    pass(&mut m, 120);
    assert_eq!(m.misses(), vec![]);
    // Without the alarm the operator is never told and the bound is broken.
    let mut late = armed_idle(params(vec![go(0)])).with_defect(Defect::NoAlarm);
    run(
        &mut late,
        &[
            Event::Occur(0),
            Event::Scan(0),
            Event::WaiterFire,
            Event::BeginTurn,
        ],
    );
    pass(&mut late, 40);
    run(&mut late, &[Event::Escalate(0)]);
    pass(&mut late, 60);
    assert_eq!(late.violations(), vec![Invariant::UrgentBound]);
    assert_eq!(late.misses(), vec![Miss::Unread(0)]);
}

#[test]
fn a_dead_role_hands_the_event_to_the_operator_by_the_bound_not_at_recovery() {
    // invariants.rs:50: with no live session a receipt at 100 s is not inside the bound.
    let mut m = armed_idle(params(vec![go(0)]));
    run(&mut m, &[Event::Occur(0), Event::Scan(0), Event::Crash]);
    pass(&mut m, 40);
    run(&mut m, &[Event::Escalate(0)]);
    pass(&mut m, 20);
    run(&mut m, &[Event::Alarm(0)]);
    assert_eq!((m.alarmed(0), m.operator_secs(0)), (true, Some(60)));
    assert_eq!(
        m.stage(0),
        Stage::Detected,
        "no session read it: it stays pending for one"
    );
    // The operator alarm does not stand in for the session: at 90 s time stops until the
    // authorized recovery replaces the dead session, and its successor reads the event.
    pass(&mut m, 30);
    assert_eq!(m.apply(Event::Tick), Err(Denied::Deadline));
    assert_eq!((m.stage(0), m.life()), (Stage::Detected, Life::Stopped));
    run(&mut m, &[Event::Recover]);
    assert_eq!(
        (m.stage(0), m.life(), m.session_secs(0)),
        (Stage::Received, Life::Idle, Some(90))
    );
    assert_eq!((m.operator_secs(0), m.misses()), (Some(60), vec![]));
    // Without the alarm and the recovery nobody holds the event past 100 s.
    let mut late = armed_idle(params(vec![go(0)]))
        .with_defect(Defect::NoAlarm)
        .with_defect(Defect::NoRecovery);
    run(&mut late, &[Event::Occur(0), Event::Scan(0), Event::Crash]);
    pass(&mut late, 40);
    run(&mut late, &[Event::Escalate(0)]);
    pass(&mut late, 60);
    assert_eq!((late.stage(0), late.age_secs(0)), (Stage::Detected, 100));
    assert_eq!(
        late.violations(),
        vec![Invariant::UrgentBound, Invariant::SessionBound]
    );
    assert_eq!(late.misses(), vec![Miss::Unread(0)]);
    // With the alarm but no recovery the session bound alone fails.
    let mut open = armed_idle(params(vec![go(0)])).with_defect(Defect::NoRecovery);
    run(&mut open, &[Event::Occur(0), Event::Scan(0), Event::Crash]);
    pass(&mut open, 40);
    run(&mut open, &[Event::Escalate(0)]);
    pass(&mut open, 20);
    run(&mut open, &[Event::Alarm(0)]);
    pass(&mut open, 40);
    assert_eq!(open.violations(), vec![Invariant::SessionBound]);
}

#[test]
fn a_hung_adapter_does_not_delay_another_source() {
    // alwayson-gpt-r2 HIGH: slow sources are isolated; no read waits on another.
    let mut m = armed_idle(params(vec![go(0), event(UrgentClass::MainRed, 1)]));
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
        &[Event::WaiterFire, Event::BeginTurn, Event::Receive],
    );
    assert_eq!(
        (m.stage(1), m.escalated(1), m.age_secs(1)),
        (Stage::Received, false, 0)
    );
}

#[test]
fn a_failed_wake_is_recovered_by_the_supervisor_inside_one_pass() {
    // alwayson-gpt-r1 HIGH (Stop and recovery): the wait died before it could wake the session.
    let mut m = armed_idle(params(vec![go(0)]));
    run(&mut m, &[Event::Occur(0), Event::WaiterDie, Event::Scan(0)]);
    assert_eq!(
        m.apply(Event::WaiterFire),
        Err(Denied::Guard),
        "a dead wait cannot wake"
    );
    ticks(&mut m, SUPERVISOR_SECS);
    assert_eq!(
        m.apply(Event::Tick),
        Err(Denied::Deadline),
        "one supervisor pass is due"
    );
    run(&mut m, &[Event::Nudge, Event::BeginTurn, Event::Receive]);
    assert_eq!((m.stage(0), m.escalated(0)), (Stage::Received, false));
    // Without the supervisor the session sleeps on.
    let mut bad = armed_idle(params(vec![])).with_defect(Defect::NoSupervisor);
    run(&mut bad, &[Event::WaiterDie]);
    ticks(&mut bad, 30);
    assert_eq!(bad.violations(), vec![Invariant::Unattended]);
}

#[test]
fn a_scan_is_not_a_receipt() {
    // A3 requirement 6: the scan cursor moves apart from the ack.
    let mut m = armed_idle(params(vec![go(0)]));
    run(&mut m, &[Event::Occur(0), Event::WaiterDie]);
    assert_eq!(m.apply(Event::Scan(0)), Ok(Outcome::Applied));
    assert_eq!(
        m.stage(0),
        Stage::Detected,
        "read into the digest, not acknowledged"
    );
}
