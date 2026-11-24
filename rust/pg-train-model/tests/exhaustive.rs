//! Every ordering of the event alphabet up to a depth bound, over four parts with one stack, an overlap
//! and a dependent child: the invariants hold after each event and a denied event changes nothing.
mod support;
use pg_train_model::*;
use std::collections::HashSet;
use support::*;

const DEPTH: usize = 9;
const PARTS: PartId = 4;
const TRAINS: TrainId = 3;

fn alphabet(m: &Model, trains: TrainId) -> Vec<Event> {
    let mut out = vec![
        Event::Tick,
        Event::Select,
        Event::StartRetry,
        Event::MainMove,
    ];
    for part in 0..PARTS {
        out.extend([
            Event::Register { part },
            Event::Drop { part },
            Event::Revert { part },
            Event::Readmit { part },
        ]);
        for train in 0..trains {
            out.extend([
                Event::Join { part, train },
                Event::Conflict { train, part },
                Event::CiRed {
                    train,
                    culprit: part,
                },
                Event::VerifyLeftOut { train, part },
                Event::RecordLeftOut {
                    train,
                    part,
                    disposition: Disposition::Target,
                },
            ]);
        }
    }
    for train in 0..trains {
        let base = m.main();
        out.extend([
            Event::Bind {
                train,
                build: train,
                base,
                policy: POLICY,
            },
            Event::Bind {
                train,
                build: train,
                base: base + 1,
                policy: POLICY,
            },
            Event::StartCi { train },
            Event::CiResult { train, green: true },
            Event::CiResult {
                train,
                green: false,
            },
            Event::Settle { train },
            Event::SettleLegacy { train },
            Event::VerifyOutcome { train },
            Event::Rebuild { train },
        ]);
    }
    out
}

fn model(policy: Policy) -> Model {
    // Parts 0 and 2 overlap; part 1 is the child of part 0; part 3 is independent.
    let specs = vec![
        spec(0b01, None),
        spec(0b10, Some(0)),
        spec(0b01, None),
        spec(0b100, None),
    ];
    Model::new(policy, specs)
}

fn explore(policy: Policy) -> (usize, [bool; 4]) {
    let mut seen: HashSet<Model> = HashSet::new();
    let mut frontier = vec![model(policy)];
    seen.insert(frontier[0].clone());
    let mut reached = [false; 4];
    for _ in 0..DEPTH {
        let mut next = Vec::new();
        for m in &frontier {
            for e in alphabet(m, TRAINS) {
                let mut n = m.clone();
                match n.apply(e.clone()) {
                    Ok(_) => assert!(n.invariants_hold(), "{e:?} broke an invariant of {m:?}"),
                    Err(_) => {
                        assert_eq!(&n, m, "{e:?} was denied but changed the model");
                        continue;
                    }
                }
                if n.train_count() > TRAINS as usize {
                    continue;
                }
                reached[0] |= (0..PARTS).any(|p| n.status(p) == Status::Landed);
                reached[1] |= (0..PARTS).any(|p| n.status(p) == Status::Held);
                reached[2] |= n.escalated_lineages() > 0;
                reached[3] |= (0..PARTS).any(|p| n.status(p) == Status::Dropped);
                if seen.insert(n.clone()) {
                    next.push(n);
                }
            }
        }
        frontier = next;
    }
    (seen.len(), reached)
}

#[test]
fn every_interleaving_keeps_the_invariants_under_each_policy() {
    let policies = [
        policy(2, 1, 1, Order::Oldest),
        policy(2, 1, 1, Order::Count),
        Policy::every_ready_rides(Order::Oldest),
    ];
    for p in policies {
        let (states, reached) = explore(p);
        assert!(states > 10_000, "only {states} states");
        assert_eq!(
            reached, [true; 4],
            "a state class was never reached under {p:?}"
        );
    }
}
