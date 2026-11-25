//! Scenarios of the urgent-event timing under the 90 s bound (rule 3.48, D-6578): per-class
//! budgets, the supervisor's retry, the operator alarm, the session deadline and the recovery.
//! Where a guard is the point, the same trace without it breaks an invariant.
mod common;
use common::*;
use pg_session_cutover_model::*;

/// The role is active and the session is working, so no supervisor pass is due.
fn working(events: Vec<EventSpec>) -> Model {
    let mut m = active(events);
    run(&mut m, "Nudge BeginTurn");
    m
}

#[test]
fn budgets_follow_the_ruling_and_each_class_has_its_own() {
    let supervisor = |secs| {
        Model::new(Params {
            supervisor_secs: secs,
            ..Params::production(vec![])
        })
    };
    for (secs, want) in [
        (300, ParamError::SupervisorTooSlow),
        (25, ParamError::Tick),
        (2550, ParamError::Range),
    ] {
        assert_eq!(supervisor(secs).err(), Some(want), "{secs} s");
    }
    let mut over = Params::production(vec![]);
    over.budgets[2].bound_secs = 100;
    assert_eq!(over.check(), Err(ParamError::OverRuling));
    let mut p = Params::production(vec![go(), spec(UrgentClass::LeaseMargin)]);
    p.budgets[UrgentClass::GoRow as usize] = Budget {
        takeover_secs: 20,
        alarm_secs: 40,
        bound_secs: 60,
    };
    let mut m = Model::new(p).unwrap();
    run(&mut m, "GrantGo Activate Nudge BeginTurn Occur0 Occur1");
    ticks(&mut m, 20);
    assert_eq!(
        try_event(&mut m, "Escalate1"),
        Err(Denied::Guard),
        "the lease class waits longer"
    );
    run(&mut m, "Escalate0");
    ticks(&mut m, 20);
    run(&mut m, "Alarm0 Escalate1");
    ticks(&mut m, 20);
    run(&mut m, "Alarm1");
    assert!(due(&mut m), "the go row is at its 60 s bound");
    run(&mut m, "Receive");
    assert_eq!(
        (m.session_secs(0), m.operator_secs(0), m.session_secs(1)),
        (Some(60), Some(40), Some(60))
    );
}

#[test]
fn a_working_session_that_ignores_an_event_is_held_to_the_bound_apart_from_the_alarm() {
    let mut m = working(vec![go()]);
    run(&mut m, "Occur0");
    ticks(&mut m, 40);
    run(&mut m, "Escalate0");
    ticks(&mut m, 20);
    run(&mut m, "Alarm0");
    assert_eq!((m.operator_secs(0), m.session_secs(0)), (Some(60), None));
    let open = m.clone();
    ticks(&mut m, 30);
    assert!(due(&mut m), "time stops at the session's bound");
    run(&mut m, "Receive");
    assert_eq!((m.stage(0), m.session_secs(0)), (Stage::Received, Some(90)));
    let mut late = open.with_defect(Defect::NoRecovery);
    ticks(&mut late, 40);
    assert_eq!(late.violations(), vec![Invariant::SessionBound]);
}

#[test]
fn starting_a_turn_at_the_bound_does_not_clear_the_session_deadline() {
    // The session is idle with every wake lost; the supervisor nudges at each pass.
    let mut m = active(vec![go()]);
    run(&mut m, "Occur0");
    for word in ["Nudge", "Escalate0 Nudge", "Alarm0 Nudge", "Nudge"] {
        ticks(&mut m, 20);
        run(&mut m, &format!("{word} WakeLost"));
    }
    ticks(&mut m, 10);
    assert!(due(&mut m), "recovery is owed at 90 s");
    run(&mut m, "Nudge BeginTurn");
    assert!(due(&mut m), "a started turn is not a receipt");
    run(&mut m, "Receive");
    ticks(&mut m, 10);
    assert_eq!(m.session_secs(0), Some(90));
}

#[test]
fn a_dead_role_is_alarmed_by_the_bound_and_recovered_at_it() {
    let mut m = active(vec![go()]);
    run(&mut m, "Occur0 Crash");
    ticks(&mut m, 40);
    run(&mut m, "Escalate0");
    ticks(&mut m, 20);
    run(&mut m, "Alarm0");
    assert_eq!(
        (m.stage(0), m.life(), m.operator_secs(0)),
        (Stage::Occurred, Life::Stopped, Some(60))
    );
    ticks(&mut m, 30);
    assert!(due(&mut m), "the alarm is not the receipt");
    run(&mut m, "Recover");
    assert_eq!(
        (m.stage(0), m.life(), m.session_secs(0)),
        (Stage::Received, Life::Idle, Some(90))
    );
    let mut late = active(vec![go()])
        .with_defect(Defect::NoAlarm)
        .with_defect(Defect::NoRecovery);
    run(&mut late, "Occur0 Crash");
    ticks(&mut late, 40);
    run(&mut late, "Escalate0");
    ticks(&mut late, 60);
    assert_eq!(
        late.violations(),
        vec![Invariant::UrgentBound, Invariant::SessionBound]
    );
}

#[test]
fn recovery_acknowledges_only_what_the_successor_receives() {
    let mut m = active(vec![go(), spec(UrgentClass::MainRed)]);
    run(&mut m, "Occur0 Crash");
    ticks(&mut m, 40);
    run(&mut m, "Escalate0");
    ticks(&mut m, 20);
    run(&mut m, "Alarm0");
    ticks(&mut m, 20);
    run(&mut m, "Occur1");
    ticks(&mut m, 10);
    assert!(due(&mut m));
    run(&mut m, "Recover");
    assert_eq!((m.stage(0), m.session_secs(0)), (Stage::Received, Some(90)));
    assert_eq!((m.stage(1), m.session_secs(1)), (Stage::Occurred, None));
}

#[test]
fn rollback_and_a_lost_go_keep_the_supervisor_retry_and_its_clock() {
    for off in ["Rollback", "RevokeGo"] {
        let mut m = active(vec![go()]);
        run(&mut m, &format!("Occur0 {off} WakeLost"));
        assert_eq!(m.mode(), Mode::Off);
        ticks(&mut m, 20);
        assert!(due(&mut m), "{off}: a pass is due at 20 s");
        run(&mut m, "Nudge");
        ticks(&mut m, 20);
        assert!(due(&mut m), "{off}: and again at 40 s");
        run(&mut m, "Nudge BeginTurn Receive");
        assert_eq!(m.session_secs(0), Some(40));
    }
}

#[test]
fn an_idle_session_is_nudged_inside_one_pass() {
    let mut m = active(vec![]).with_defect(Defect::NoSupervisor);
    ticks(&mut m, 30);
    assert_eq!(m.violations(), vec![Invariant::Unattended]);
}
