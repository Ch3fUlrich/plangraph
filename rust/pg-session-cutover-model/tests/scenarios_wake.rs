//! Scenarios of the wake path: bound, hung adapter, failed wake, digest, Stop hook.
//! One named scenario per cutover finding (state-gpt-r1, r2, alwayson-gpt-r1 to r4) and canary fact.
mod common;
use common::*;
use pg_session_cutover_model::*;

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
            of_class(Class::Beat, 0),
            of_class(Class::FollowUp, 1),
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
fn an_escalated_event_is_read_inside_the_bound_even_in_an_active_turn() {
    // gpt-6.2-sol-high round 1 HIGH: the bound ends at the session's receipt, not at a queue.
    let mut m = armed_ready(params(vec![URGENT(0)], 0));
    // The session is in a turn that never reads; the event sits on a hung source.
    run(
        &mut m,
        &[
            Event::Hang(0),
            Event::Occur(0),
            Event::WindowEnd(0),
            Event::BeginTurn(0),
        ],
    );
    ticks(&mut m, 60);
    scan_all(&mut m);
    ticks(&mut m, 10);
    run(&mut m, &[Event::Escalate(0)]);
    assert_eq!(
        m.apply(Event::Tick),
        Err(Denied::Deadline),
        "the working session must read it"
    );
    run(&mut m, &[Event::Receive(0)]);
    assert!(m.stage(0) == Stage::Received && m.age_secs(0) <= URGENT_BOUND_SECS);
    // Without the wait for the read the event passes the bound unreceived in the turn.
    let mut late = armed_ready(params(vec![URGENT(0)], 0)).with_defect(Defect::FallbackNotAwaited);
    run(
        &mut late,
        &[
            Event::Hang(0),
            Event::Occur(0),
            Event::WindowEnd(0),
            Event::BeginTurn(0),
        ],
    );
    ticks(&mut late, 60);
    scan_all(&mut late);
    ticks(&mut late, 10);
    run(&mut late, &[Event::Escalate(0)]);
    ticks(&mut late, 30);
    assert_eq!(late.violations(), vec![Invariant::UrgentBound]);
}
