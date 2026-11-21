use std::collections::{HashSet, VecDeque};

use super::*;

const DEPTH: usize = 11;

const STEPS: &[Step] = &[
    Step::Bind(42),
    Step::Dispatch,
    Step::Expire,
    Step::Finish(true),
    Step::Finish(false),
    Step::Observe(Proof::Present),
    Step::Observe(Proof::AbsentNow),
    Step::Observe(Proof::FencedAbsent),
    Step::Observe(Proof::Undecidable),
    Step::Resolve(1),
];

#[test]
fn caller_successor_destinations_crash_and_router_interleave() {
    for class in [Class::Gated, Class::Foreign, Class::Budgeted] {
        let budget = u32::from(class == Class::Budgeted);
        let mut start = Ledger::default();
        start
            .begin(
                1,
                Spec {
                    resends: budget,
                    ..Spec::new(class, 7)
                },
                &[],
            )
            .unwrap();
        let mut seen = HashSet::from([start.clone()]);
        let mut frontier = VecDeque::from([(start, 0)]);
        while let Some((state, depth)) = frontier.pop_front() {
            if depth == DEPTH {
                continue;
            }
            let mut successors = Vec::new();
            let mut clock = state.clone();
            clock.tick();
            successors.push(clock);
            let mut recovered = state.clone(); // crash drops caller state, keeps rows and in-flight tickets
            if recovered.takeover(state.generation, true).is_ok() {
                successors.push(recovered);
            }
            let mut conflict = state.clone();
            if conflict.begin(2, Spec::new(class, 7), &[]).is_ok() {
                assert!(state.rows[&1].settled(), "{state:?}");
            }
            let row = &state.rows[&1];
            let current_stamp = state.stamp(1).unwrap();
            let mut runnable = false;
            for step in STEPS {
                for generation in 0..=state.generation {
                    for attempt in 0..=current_stamp.1 {
                        let mut next = state.clone();
                        let stamp = Stamp(generation, attempt);
                        if next.apply(1, stamp, *step).is_ok() {
                            assert_eq!(stamp, current_stamp);
                            assert_eq!(generation, state.generation);
                            let updated = &next.rows[&1];
                            if row.settled() {
                                assert_eq!(updated.state, row.state);
                            }
                            if updated.state == State::Failed {
                                assert!(updated.safe());
                                assert_eq!(updated.effects(), 0);
                            }
                            runnable |= next != state;
                            successors.push(next);
                        }
                    }
                }
            }
            for &(ticket, _) in &row.delivery {
                let mut destination = state.clone();
                for act in [
                    Ledger::admit,
                    Ledger::deliver,
                    Ledger::effect,
                    Ledger::fence,
                ] {
                    if act(&mut destination, ticket).is_ok() {
                        assert!(destination.effects(1) <= 1 + budget as usize);
                        runnable = true;
                        successors.push(destination.clone());
                    }
                    destination.clone_from(&state);
                }
            }
            let due = row.disposition.is_none()
                && match row.state {
                    State::Pending | State::Unknown => true,
                    State::Dispatching | State::Admitted => state.now >= row.deadline,
                    _ => false,
                };
            assert_eq!(state.obligations().contains(&1), due);
            assert!(!due || runnable, "{state:?}");
            for next in successors {
                if seen.insert(next.clone()) {
                    frontier.push_back((next, depth + 1));
                }
            }
        }
        assert!(
            seen.len() > 100,
            "only {} states explored for {class:?}",
            seen.len()
        );
    }
}

#[test]
fn manifest_retry_and_cancel_interleavings_cover_owed_rows() {
    let mut start = Ledger::default();
    let child = Spec {
        retryable: true,
        ..Spec::new(Class::Gated, 8)
    };
    start
        .begin(1, Spec::new(Class::Launch, 7), &[(2, child)])
        .unwrap();
    start.apply(1, Stamp(0, 0), Step::Bind(42)).unwrap();
    let Reply::Send(ticket) = start.apply(1, Stamp(0, 0), Step::Dispatch).unwrap() else {
        panic!("launch not dispatched");
    };
    start.admit(ticket).unwrap();
    start.effect(ticket).unwrap();
    start.apply(1, Stamp(0, 1), Step::Finish(true)).unwrap();
    start.apply(2, Stamp(0, 0), Step::Bind(42)).unwrap();

    let mut seen = HashSet::from([start.clone()]);
    let mut frontier = VecDeque::from([(start, 0)]);
    let mut coverage = [false; 4];
    while let Some((state, depth)) = frontier.pop_front() {
        if depth == 8 {
            continue;
        }
        let mut next_states = Vec::new();
        let mut clock = state.clone();
        clock.tick();
        next_states.push(clock);
        let mut successor = state.clone();
        if state.generation == 0 && successor.takeover(0, true).is_ok() {
            next_states.push(successor);
        }
        let obligations = state.obligations();
        for (&id, row) in &state.rows {
            let due_parent = row
                .parent
                .is_some_and(|parent| state.state(parent) == Some(State::Ok));
            let due = row.disposition.is_none()
                && match row.state {
                    State::Planned => due_parent,
                    State::Failed => due_parent && row.replaced_by.is_none(),
                    State::Pending | State::Unknown => true,
                    State::Dispatching | State::Admitted => state.now >= row.deadline,
                    State::Ok => false,
                };
            assert_eq!(obligations.contains(&id), due);
            let mut runnable = false;
            let stamp = state.stamp(id).unwrap();
            let extra = [Step::Activate, Step::RetryChild(3), Step::RequestCancel(4)];
            for &step in STEPS.iter().chain(&extra) {
                let mut next = state.clone();
                if stamp.0 > 0 {
                    assert!(next.apply(id, Stamp(stamp.0 - 1, stamp.1), step).is_err());
                }
                if next.apply(id, stamp, step).is_ok() && next != state {
                    let updated = &next.rows[&id];
                    assert!(!row.settled() || updated.state == row.state);
                    assert!(
                        updated.state != State::Failed
                            || (updated.safe() && updated.effects() == 0)
                    );
                    coverage[0] |= step == Step::Activate;
                    coverage[1] |= step == Step::RetryChild(3);
                    coverage[2] |= step == Step::RequestCancel(4);
                    coverage[3] |= id == 4 && updated.state == State::Ok && stamp.0 > 0;
                    runnable = true;
                    next_states.push(next);
                }
            }
            for &(ticket, _) in &row.delivery {
                for act in [
                    Ledger::admit,
                    Ledger::effect,
                    Ledger::fence,
                    Ledger::confirm_stopped,
                ] {
                    let mut next = state.clone();
                    if act(&mut next, ticket).is_ok() && next != state {
                        assert!(next.effects(id) <= 1);
                        runnable = true;
                        next_states.push(next);
                    }
                }
            }
            if let Some(conflict) = row.spec.conflict {
                let mut next = state.clone();
                if next
                    .begin(5, Spec::new(Class::Gated, conflict), &[])
                    .is_ok()
                {
                    assert!(row.settled() || state.inactive_child(row));
                }
            }
            assert!(!due || runnable, "owed row {id} cannot run in {state:?}");
        }
        for next in next_states {
            if seen.insert(next.clone()) {
                frontier.push_back((next, depth + 1));
            }
        }
    }
    assert_eq!(
        coverage, [true; 4],
        "activation, retry, cancel and successor settlement"
    );
}
