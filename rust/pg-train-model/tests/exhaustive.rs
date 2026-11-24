//! Every ordering of the event alphabet up to a depth bound, over four parts: a stack of three with an
//! overlap, and an independent part: the invariants hold after each event and a denied event changes nothing.
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
        Event::Restart,
    ];
    for part in 0..PARTS {
        out.extend([
            Event::Register { part },
            Event::Drop { part },
            Event::Revert { part },
            Event::Readmit { part },
        ]);
        for train in 0..trains.min(2) {
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
            Event::Amend { train },
            Event::FixInPlace { train },
            Event::CiRedFix { train },
            Event::Speculate { pred: train },
            Event::Promote { train },
            Event::VerifyComplete { train },
        ]);
    }
    out
}

fn model(policy: Policy) -> Model {
    // A stack 0 <- 1 <- 2 (parts 0 and 2 overlap) and an independent part 3.
    let specs = vec![
        spec(0b01, None),
        spec(0b10, Some(0)),
        spec(0b01, Some(1)),
        spec(0b100, None),
    ];
    Model::new(policy, specs)
}

/// A train under test with a part waiting: the start of every speculative path.
fn pipeline(policy: Policy) -> Model {
    let mut m = model(policy);
    for e in [
        Event::Register { part: 3 },
        Event::Tick,
        Event::Select,
        Event::Bind {
            train: 0,
            build: 0,
            base: 0,
            policy: POLICY,
        },
        Event::StartCi { train: 0 },
        Event::Register { part: 0 },
        Event::Tick,
    ] {
        m.apply(e).unwrap();
    }
    m
}

fn explore(policy: Policy) -> (usize, [bool; 5]) {
    let mut seen: HashSet<Model> = HashSet::new();
    let mut frontier = vec![model(policy), pipeline(policy)];
    frontier.iter().for_each(|m| {
        seen.insert(m.clone());
    });
    let mut reached = [false; 5];
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
                reached[4] |= n.promoted() > 0;
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
            reached, [true; 5],
            "a state class was never reached under {p:?}"
        );
    }
}
