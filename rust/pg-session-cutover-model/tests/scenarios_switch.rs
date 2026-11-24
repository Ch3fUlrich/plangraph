//! Scenarios of the role switch: activation gates, rollback and loss of the GO.
//! One named scenario per cutover finding (state-gpt-r1, r2, alwayson-gpt-r1 to r4) and canary fact.
mod common;
use common::*;
use pg_session_cutover_model::*;

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
