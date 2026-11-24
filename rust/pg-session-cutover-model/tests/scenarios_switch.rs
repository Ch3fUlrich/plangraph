//! Scenarios of the role switch: activation gates, an overdue event, rollback, loss of the GO.
mod common;
use common::*;
use pg_session_cutover_model::*;

/// A role in shadow mode with every gate but the switch itself, and a session that works unread.
fn shadow_with_working_session() -> Model {
    let mut m = model(params(vec![go(0)]));
    run(
        &mut m,
        &[Event::StartShadow, Event::SignOffShadow, Event::DeployFence],
    );
    run(
        &mut m,
        &[
            Event::DeployRecovery,
            Event::GrantGo,
            Event::Occur(0),
            Event::BeginTurn,
        ],
    );
    m
}

#[test]
fn activation_is_refused_with_an_urgent_event_that_is_already_overdue() {
    // transition.rs:177: reactivation must not start a role whose bound is already broken.
    let mut m = shadow_with_working_session();
    pass(&mut m, 50);
    assert_eq!(
        m.apply(Event::Activate),
        Ok(Outcome::Applied),
        "50 s old: the alarm is still ahead"
    );
    run(
        &mut m,
        &[Event::Rollback, Event::StartShadow, Event::SignOffShadow],
    );
    pass(&mut m, 20);
    assert_eq!(m.age_secs(0), 70);
    assert_eq!(
        m.apply(Event::Activate),
        Err(Denied::Overdue),
        "past its alarm age"
    );
    // Once a reader holds it the role may start.
    run(&mut m, &[Event::Receive, Event::Activate]);
    // With no look at the event the role starts already past its bound.
    let mut bad = shadow_with_working_session().with_defect(Defect::ActivateOverdue);
    pass(&mut bad, 100);
    assert_eq!(bad.apply(Event::Activate), Ok(Outcome::Applied));
    assert_eq!(
        bad.violations(),
        vec![Invariant::UrgentBound, Invariant::SessionUnread]
    );
}

#[test]
fn reactivation_after_a_rollback_with_an_old_event_waits_for_a_reader() {
    // transition.rs:177, the rollback path: an event handed to today's path and left unread.
    let mut m = armed_idle(params(vec![go(0)]));
    run(
        &mut m,
        &[
            Event::Occur(0),
            Event::Scan(0),
            Event::Rollback,
            Event::BeginTurn,
        ],
    );
    run(&mut m, &[Event::StartShadow, Event::SignOffShadow]);
    pass(&mut m, 60);
    assert_eq!(m.apply(Event::Activate), Err(Denied::Overdue));
    run(&mut m, &[Event::Receive, Event::Activate]);
}

#[test]
fn activation_needs_shadow_fence_recovery_and_a_live_go() {
    // A3 requirements 7 and 7a: the design switches nothing by itself.
    let mut m = model(params(vec![]));
    assert_eq!(m.apply(Event::Activate), Err(Denied::Mode));
    run(&mut m, &[Event::StartShadow]);
    for gate in [
        Event::SignOffShadow,
        Event::DeployFence,
        Event::DeployRecovery,
        Event::GrantGo,
    ] {
        assert_eq!(m.apply(Event::Activate), Err(Denied::Gate));
        run(&mut m, &[gate]);
    }
    run(&mut m, &[Event::Activate]);
    assert_eq!(m.mode(), Mode::Active);
    // Without the gates the role would be active unfenced.
    let mut bad = model(params(vec![])).with_defect(Defect::ActivateWithoutGates);
    run(&mut bad, &[Event::StartShadow]);
    assert_eq!(bad.apply(Event::Activate), Ok(Outcome::Applied));
    assert_eq!(bad.violations(), vec![Invariant::ActivationGate]);
}

#[test]
fn rollback_and_a_lost_go_hand_every_unread_event_to_todays_wake_path() {
    // A3 requirement 7: the rollback switch returns to today's behaviour.
    let mut m = armed_idle(params(vec![go(0)]));
    run(
        &mut m,
        &[
            Event::Occur(0),
            Event::WaiterDie,
            Event::Scan(0),
            Event::Rollback,
        ],
    );
    assert_eq!(
        (m.mode(), m.escalated(0), m.notified()),
        (Mode::Off, true, true)
    );
    run(&mut m, &[Event::BeginTurn]);
    assert_eq!(
        m.apply(Event::Arm),
        Err(Denied::Mode),
        "no digest wait while off"
    );
    run(&mut m, &[Event::Receive]);
    assert_eq!(m.stage(0), Stage::Received);
    // Losing the GO of an active role does the same.
    let mut m = armed_idle(params(vec![]));
    run(&mut m, &[Event::RevokeGo]);
    assert_eq!(m.mode(), Mode::Off);
    assert_eq!(
        m.apply(Event::Activate),
        Err(Denied::Mode),
        "a new shadow sign-off and GO are needed"
    );
}

#[test]
fn an_unswitched_role_keeps_todays_wake_path() {
    let mut m = model(params(vec![go(0)]));
    run(&mut m, &[Event::Occur(0)]);
    assert!(
        m.escalated(0) && m.notified(),
        "nothing waits on a digest before the switch"
    );
}
