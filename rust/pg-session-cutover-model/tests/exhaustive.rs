//! Every ordering of the events of a stated alphabet, up to a stated number of events.
//!
//! Bounds (also in docs/session-cutover-model.md): one role; Stop-hook cap 2 (1 in the
//! two-recycle search, 9 in a named scenario); times are the production ones (scan 60 s, takeover 70 s, supervisor 20 s,
//! bound 90 s, tick 10 s).
//!   wake:     1 session, 2 urgent events on 2 sources, no messages, 13 events deep (no heal, crash, tool use or ready marker).
//!   recycle:  2 sessions (one recycle), 1 urgent event, 2 direct messages, 11 events deep.
//!   recycles: 3 sessions (two recycles), 1 direct message, crashes, 16 events deep.
//!   gates:    all gate and mode events with 1 urgent event and 2 sessions, 15 events deep.
//! The first three start from an activated role; `gates` starts from the role switched off.
mod common;
use common::*;
use pg_session_cutover_model::*;

fn search(
    seed: Model,
    alpha: Vec<Event>,
    depth: usize,
    mut edge: impl FnMut(&Model, Event, &Result<Outcome, Denied>, &Model),
) {
    let r = explore(seed, alpha, depth, |a, e, r, b| edge(a, e, r, b));
    assert_eq!(r.violation, None, "{} states", r.states);
    assert_eq!(r.max_depth, depth, "the bound was not reached");
    assert!(r.states > 1000);
}

fn skip_cron_and_window(e: &Event) -> bool {
    matches!(
        e,
        Event::ArmCron(_) | Event::ClearCron(_) | Event::WindowEnd(_)
    )
}

#[test]
fn wake_orderings_keep_every_invariant() {
    let mut p = params(vec![URGENT(0), URGENT(1)], 0);
    p.sessions = 1;
    p.stop_cap = 2;
    digest_of(&mut p, 1);
    let mut m = model(p);
    activate(&mut m);
    let alpha = alphabet_without(&m, |e| {
        (is_gate(e) && *e != Event::Rollback)
            || is_recycle(e)
            || is_message(e)
            || skip_cron_and_window(e)
            || matches!(
                e,
                Event::Heal(_) | Event::ToolUse(_) | Event::MarkReady(_) | Event::Crash(_)
            )
    });
    let mut seen = [false; 8];
    search(m, alpha, 13, |a, e, r, b| {
        let ok = r.is_ok();
        seen[0] |= ok && matches!(e, Event::WaiterFire(_));
        seen[1] |= ok && matches!(e, Event::Escalate(_));
        seen[2] |= matches!(r, Ok(Outcome::Blocked));
        // The cap ended a turn with no armed wait: the Stop hook is a help, not a proof.
        seen[3] |= ok
            && matches!(e, Event::EndTurn(_))
            && a.waiter(0) != Waiter::Armed
            && a.mode() == Mode::Active;
        seen[4] |= ok && e == Event::Nudge;
        seen[5] |= ok
            && e == Event::Rollback
            && (0..2).any(|i| a.stage(i) != Stage::Received && a.stage(i) != Stage::Future);
        seen[6] |= *r == Err(Denied::Deadline);
        // A hung source 0 does not stop source 1 from being read and received.
        seen[7] |=
            ok && matches!(e, Event::Receive(_)) && a.hung(0) && b.stage(1) == Stage::Received;
    });
    assert_eq!(seen, [true; 8]);
}

#[test]
fn recycle_orderings_keep_every_invariant() {
    let mut p = params(vec![URGENT(0)], 2);
    p.stop_cap = 2;
    let mut m = model(p);
    activate(&mut m);
    let alpha = alphabet_without(&m, |e| {
        is_gate(e) || skip_cron_and_window(e) || matches!(e, Event::Hang(_) | Event::Heal(_))
    });
    let mut seen = [false; 6];
    search(m, alpha, 11, |a, e, r, b| {
        let ok = r.is_ok();
        seen[0] |= ok && e == Event::StopOk;
        seen[1] |= *r == Ok(Outcome::StopFailed);
        seen[2] |= ok
            && e == Event::Adopt
            && (0..2).any(|i| a.home(i) == Home::Session(0) && b.home(i) == Home::Session(1));
        seen[3] |= ok && matches!(e, Event::HandleMsg { .. });
        seen[4] |= *r == Err(Denied::Active) && e == Event::BeginRecycle;
        // Adoption revokes the predecessor's armed wait.
        seen[5] |=
            ok && e == Event::Adopt && a.waiter(0) == Waiter::Armed && b.waiter(0) == Waiter::Dead;
    });
    assert_eq!(seen, [true; 6]);
}

#[test]
fn two_recycles_and_crashes_keep_every_invariant() {
    let mut p = params(vec![], 1);
    p.sessions = 3;
    p.stop_cap = 1;
    let mut m = model(p);
    activate(&mut m);
    let alpha = alphabet_without(&m, |e| {
        is_gate(e)
            || skip_cron_and_window(e)
            || matches!(
                e,
                Event::Hang(_) | Event::Heal(_) | Event::Escalate(_) | Event::Occur(_)
            )
    });
    let mut second = false;
    search(m, alpha, 16, |_, e, r, b| {
        second |= r.is_ok() && e == Event::Adopt && b.adopted() == 2
    });
    assert!(second, "no second recycle was reached");
}

#[test]
fn gate_and_mode_orderings_keep_every_invariant() {
    let mut p = params(vec![URGENT(0)], 0);
    p.stop_cap = 2;
    let m = model(p);
    let alpha = alphabet_without(&m, |e| {
        is_recycle(e)
            || is_message(e)
            || matches!(
                e,
                Event::Hang(_)
                    | Event::Heal(_)
                    | Event::WindowEnd(_)
                    | Event::WaiterDie(_)
                    | Event::Crash(_)
                    | Event::MarkReady(_)
            )
    });
    let mut seen = [false; 5];
    search(m, alpha, 15, |a, e, r, b| {
        seen[0] |= r.is_ok() && e == Event::Activate;
        seen[1] |= *r == Err(Denied::Gate) && e == Event::Activate;
        seen[2] |= *r == Err(Denied::Cron) && e == Event::Activate;
        seen[3] |=
            r.is_ok() && e == Event::RevokeGo && a.mode() == Mode::Active && b.mode() == Mode::Off;
        seen[4] |= *r == Err(Denied::Cron) && matches!(e, Event::ArmCron(_));
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
            Event::WaiterFire(_)
                | Event::BeginTurn(_)
                | Event::Scan(_)
                | Event::Escalate(_)
                | Event::Nudge
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

fn is_turn(e: &Event) -> bool {
    matches!(
        e,
        Event::Nudge
            | Event::BeginTurn(_)
            | Event::Arm(_)
            | Event::EndTurn(_)
            | Event::MarkReady(_)
    )
}

fn is_cycle(e: &Event) -> bool {
    matches!(
        e,
        Event::BeginRecycle | Event::Confirm | Event::Adopt | Event::StopOk
    )
}

#[test]
fn each_defect_is_caught_by_its_invariant() {
    let wake = |e: &Event| {
        matches!(
            e,
            Event::Tick
                | Event::Occur(_)
                | Event::Hang(_)
                | Event::Scan(_)
                | Event::Receive(_)
                | Event::Nudge
                | Event::BeginTurn(_)
                | Event::Arm(_)
                | Event::EndTurn(_)
        )
    };
    assert!(finds(
        Defect::NoTakeover,
        params(vec![URGENT(0)], 0),
        true,
        wake,
        19
    )
    .contains(&Invariant::UrgentBound));
    assert!(finds(
        Defect::FallbackNotAwaited,
        params(vec![URGENT(0)], 0),
        true,
        wake,
        20
    )
    .contains(&Invariant::UrgentBound));
    let scans = |e: &Event| matches!(e, Event::Occur(_) | Event::Scan(_));
    assert!(finds(
        Defect::ScanIsReceipt,
        params(vec![URGENT(0)], 0),
        true,
        scans,
        3
    )
    .contains(&Invariant::ReceivedUnread));
    let mut digest = params(vec![URGENT(0), URGENT(0), URGENT(0)], 0);
    digest_of(&mut digest, 2);
    let reads =
        |e: &Event| matches!(e, Event::Occur(_) | Event::Scan(_) | Event::Receive(_)) || is_turn(e);
    assert!(finds(Defect::DigestUnbounded, digest, true, reads, 8)
        .contains(&Invariant::DigestOverBudget));
    let mut footer = params(vec![URGENT(0), URGENT(0)], 0);
    digest_of(&mut footer, 2);
    assert!(finds(Defect::FooterRange, footer, true, reads, 8).contains(&Invariant::RangeNotFirst));
    let cycle = |e: &Event| is_turn(e) || is_cycle(e);
    let recycle = || params(vec![], 0);
    assert!(
        finds(Defect::RecycleWhileActive, recycle(), true, cycle, 11)
            .contains(&Invariant::AdoptedOverActive)
    );
    assert!(finds(Defect::NoFence, recycle(), true, cycle, 10).contains(&Invariant::SingleCurrent));
    let msg =
        |e: &Event| is_turn(e) || is_cycle(e) || is_message(e) || matches!(e, Event::ToolUse(_));
    assert!(finds(
        Defect::LaterToolCallHandles,
        params(vec![], 1),
        true,
        msg,
        6
    )
    .contains(&Invariant::HandledWithoutAction));
    assert!(
        finds(Defect::CarrySnapshotOnly, params(vec![], 1), true, msg, 11)
            .contains(&Invariant::MessageStranded)
    );
    let gates = |e: &Event| is_gate(e);
    assert!(finds(
        Defect::ActivateWithoutGates,
        params(vec![], 0),
        false,
        gates,
        3
    )
    .contains(&Invariant::ActivationGate));
    let idle = |e: &Event| is_turn(e) || matches!(e, Event::Tick);
    assert!(
        finds(Defect::NoSupervisor, params(vec![], 0), true, idle, 8)
            .contains(&Invariant::Unattended)
    );
}
