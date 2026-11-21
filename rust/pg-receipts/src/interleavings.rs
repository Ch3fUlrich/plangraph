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
