//! Every ordering of the events of a stated alphabet, up to a stated number of events.
//!
//! Bounds (also in docs/session-cutover-model.md): one role with one session; production times
//! (scan 60 s, supervisor pass 20 s, takeover 40 s, alarm 60 s, bound 90 s, tick 10 s).
//!   wake:    2 urgent events of two classes on two sources, activated role, 14 events deep.
//!   classes: a go row with a 60 s budget and a lease margin with 90 s, activated role, 12 deep.
//!   gates:   all gate and mode events with 1 urgent event, role switched off, 15 events deep.
mod common;
use common::*;
use pg_session_cutover_model::*;

type Edge<'a> = &'a mut dyn FnMut(&Model, Event, &Result<Outcome, Denied>, &Model);

fn search(seed: Model, alpha: Vec<Event>, depth: usize, edge: Edge) {
    let r = explore(seed, alpha, depth, |a, e, r, b| edge(a, e, r, b));
    assert_eq!(r.violation, None, "{} states", r.states);
    assert_eq!(r.max_depth, depth, "the bound was not reached");
    assert!(r.states > 1000);
}

#[test]
fn wake_orderings_keep_every_invariant() {
    let mut m = model(params(vec![go(0), event(UrgentClass::MainRed, 1)]));
    activate(&mut m);
    let alpha = alphabet_without(&m, |e| is_gate(e) && *e != Event::Rollback);
    let mut seen = [false; 11];
    search(m, alpha, 14, &mut |a, e, r, b| {
        let ok = r.is_ok();
        seen[0] |= ok && e == Event::WaiterFire;
        seen[1] |= ok && matches!(e, Event::Escalate(_));
        seen[2] |= ok && matches!(e, Event::Alarm(_));
        seen[3] |= ok && e == Event::Nudge;
        seen[4] |= ok && e == Event::Rollback && (0..2).any(|i| a.stage(i) == Stage::Detected);
        seen[5] |= *r == Err(Denied::Deadline);
        // A hung source 0 does not stop source 1 from being read and received by the session.
        seen[6] |= ok && e == Event::Receive && a.hung(0) && b.stage(1) == Stage::Received;
        // A woken session that keeps working lets the alarm hold the bound: time passed.
        seen[7] |= ok && matches!(e, Event::Alarm(_)) && a.life() == Life::Active;
        // A session that ignores a pending event misses its deadline; nothing ends it.
        seen[8] |= ok && b.misses().contains(&Miss::Unread(0)) && b.life() == Life::Active;
        seen[9] |= ok && e == Event::WakeLost;
        // The authorized recovery replaces an idle or dead session that read nothing.
        seen[10] |= ok && e == Event::Recover && a.life() != Life::Active;
    });
    assert_eq!(seen, [true; 11]);
}

#[test]
fn per_class_budgets_keep_every_invariant() {
    let mut p = params(vec![go(0), event(UrgentClass::LeaseMargin, 1)]);
    p.budgets[UrgentClass::GoRow as usize] = Budget {
        takeover_secs: 20,
        alarm_secs: 40,
        bound_secs: 60,
    };
    let mut m = model(p);
    activate(&mut m);
    let alpha = alphabet_without(&m, is_gate);
    let mut alarmed_early = false;
    search(m, alpha, 12, &mut |a, e, r, _| {
        // The go row is alarmed at 40 s, while the lease margin of the same age is still open.
        alarmed_early |= r.is_ok()
            && e == Event::Alarm(0)
            && a.age_secs(0) == 40
            && a.stage(1) != Stage::Received;
    });
    assert!(alarmed_early);
}

#[test]
fn gate_and_mode_orderings_keep_every_invariant() {
    let m = model(params(vec![go(0)]));
    let alpha = alphabet_without(&m, |e| {
        matches!(
            e,
            Event::Hang(_) | Event::Heal(_) | Event::WaiterDie | Event::Crash
        )
    });
    let mut seen = [false; 5];
    search(m, alpha, 15, &mut |a, e, r, b| {
        seen[0] |= r.is_ok() && e == Event::Activate;
        seen[1] |= *r == Err(Denied::Gate) && e == Event::Activate;
        seen[2] |= *r == Err(Denied::Overdue) && e == Event::Activate;
        seen[3] |=
            r.is_ok() && e == Event::RevokeGo && a.mode() == Mode::Active && b.mode() == Mode::Off;
        seen[4] |= r.is_ok() && e == Event::Rollback && a.mode() == Mode::Active;
    });
    assert_eq!(seen, [true; 5]);
}

/// A model with the defect reaches the invariant; the same model without it never does.
fn finds(
    defect: Defect,
    p: Params,
    activated: bool,
    keep: impl Fn(&Event) -> bool,
    depth: usize,
) -> Vec<Invariant> {
    let mut seed = model(p);
    if activated {
        activate(&mut seed);
    }
    // Keep the events that discharge a due obligation, or time would have no way forward.
    let due = |e: &Event| {
        matches!(
            e,
            Event::WaiterFire
                | Event::BeginTurn
                | Event::Scan(_)
                | Event::Escalate(_)
                | Event::Alarm(_)
                | Event::Nudge
                | Event::Recover
        )
    };
    let alpha: Vec<Event> = seed
        .alphabet()
        .into_iter()
        .filter(|e| keep(e) || due(e))
        .collect();
    let control = explore(seed.clone(), alpha.clone(), depth, |_, _, _, _| {});
    assert_eq!(control.violation, None, "the safe model broke an invariant");
    let r = explore(seed.with_defect(defect), alpha, depth, |_, _, _, _| {});
    r.violation
        .unwrap_or_else(|| panic!("{defect:?} went unnoticed"))
        .1
}

#[test]
fn each_defect_is_caught_by_its_invariant() {
    let turn = |e: &Event| {
        matches!(
            e,
            Event::Nudge | Event::BeginTurn | Event::Arm | Event::EndTurn
        )
    };
    let read = |e: &Event| turn(e) || matches!(e, Event::Tick | Event::Occur(_) | Event::Hang(_));
    assert!(finds(Defect::NoAlarm, params(vec![go(0)]), true, read, 18)
        .contains(&Invariant::UrgentBound));
    assert!(
        finds(Defect::NoRecovery, params(vec![go(0)]), true, read, 20)
            .contains(&Invariant::SessionBound)
    );
    let idle = |e: &Event| matches!(e, Event::Tick) || turn(e);
    assert!(
        finds(Defect::NoSupervisor, params(vec![]), true, idle, 8).contains(&Invariant::Unattended)
    );
    let gates = |e: &Event| is_gate(e);
    assert!(finds(
        Defect::ActivateWithoutGates,
        params(vec![]),
        false,
        gates,
        3
    )
    .contains(&Invariant::ActivationGate));
    let overdue = |e: &Event| {
        is_gate(e) && *e != Event::Rollback
            || matches!(e, Event::Tick | Event::Occur(_) | Event::BeginTurn)
    };
    assert!(finds(
        Defect::ActivateOverdue,
        params(vec![go(0)]),
        false,
        overdue,
        20
    )
    .contains(&Invariant::UrgentBound));
}
