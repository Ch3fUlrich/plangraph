use pg_launch_recovery_model::*;

#[test]
fn delayed_forward_is_fenced_after_resolve() {
    let mut model = Model::default();
    let id = 0;
    assert_eq!(model.apply(id, Event::Reserve), Ok(Outcome::Applied));
    assert_eq!(model.apply(id, Event::Forward), Ok(Outcome::Applied));
    assert_eq!(
        model.apply(id, Event::Resolve),
        Ok(Outcome::Phase(Phase::Closed))
    );
    assert_eq!(
        model.apply(
            id,
            Event::Receive {
                digest: 0,
                stale: false,
                name_taken: false
            }
        ),
        Ok(Outcome::Phase(Phase::Closed))
    );
    assert_eq!(model.apply(id, Event::TakeStart), Err(Denied::Guard));
}

#[derive(serde::Deserialize)]
struct Vectors {
    version: u8,
    scenarios: Vec<Scenario>,
}

#[derive(serde::Deserialize)]
struct Scenario {
    name: String,
    admission_verified: bool,
    steps: Vec<Step>,
}

#[derive(serde::Deserialize)]
struct Step {
    event: Event,
    expect: Expected,
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "snake_case")]
enum Expected {
    Ok(Outcome),
    Err(Denied),
}

const SPEC: Spec = Spec {
    origin: 0,
    target: 1,
    seat: 0,
    name: 0,
    digest: 0,
    launcher: 0,
};
const RECEIVE: Event = Event::Receive {
    digest: 0,
    stale: false,
    name_taken: false,
};

#[test]
fn exported_review_scenarios() {
    let vectors: Vectors =
        serde_json::from_str(include_str!("../fixtures/transitions.json")).unwrap();
    assert_eq!(vectors.version, 1);
    assert_eq!(vectors.scenarios.len(), 10);
    for scenario in vectors.scenarios {
        let mut model = Model::new([SPEC], scenario.admission_verified, 600);
        for (index, step) in scenario.steps.into_iter().enumerate() {
            let expected = match step.expect {
                Expected::Ok(outcome) => Ok(outcome),
                Expected::Err(denied) => Err(denied),
            };
            assert_eq!(
                model.apply(0, step.event),
                expected,
                "{} step {index}: {:?}",
                scenario.name,
                step.event
            );
            assert!(model.invariants_hold(), "{} step {index}", scenario.name);
        }
    }
}

fn apply_all(model: &mut Model, id: usize, events: &[Event]) {
    for &event in events {
        assert!(model.apply(id, event).is_ok(), "{event:?}");
    }
}

#[test]
fn same_origin_cannot_forward_to_new_target_after_lost_answer() {
    let mut model = Model::new(
        [
            SPEC,
            Spec {
                target: 2,
                name: 1,
                ..SPEC
            },
        ],
        true,
        600,
    );
    apply_all(
        &mut model,
        0,
        &[Event::Reserve, Event::Forward, Event::LaunchOutcomeUnknown],
    );
    assert_eq!(model.apply(1, Event::Reserve), Err(Denied::SeatBusy));
    assert_eq!(
        model.apply(0, Event::Resolve),
        Ok(Outcome::Phase(Phase::Closed))
    );
    apply_all(
        &mut model,
        0,
        &[
            Event::StoreAnswer {
                attempt: 0,
                target: 1,
                phase: Phase::Closed,
            },
            Event::MirrorAndClose,
        ],
    );
    assert_eq!(model.apply(1, Event::Reserve), Ok(Outcome::Applied));
}

#[test]
fn target_serializes_two_origins_and_rejects_a_different_payload_on_replay() {
    let mut model = Model::new([SPEC, Spec { origin: 2, ..SPEC }], true, 600);
    apply_all(&mut model, 0, &[Event::Reserve, Event::Forward]);
    apply_all(&mut model, 1, &[Event::Reserve, Event::Forward]);
    assert_eq!(model.apply(0, RECEIVE), Ok(Outcome::Phase(Phase::Accepted)));
    assert_eq!(model.apply(1, RECEIVE), Ok(Outcome::Phase(Phase::Failed)));
    assert_eq!(
        model.apply(
            0,
            Event::Receive {
                digest: 1,
                stale: false,
                name_taken: false
            }
        ),
        Err(Denied::Conflict)
    );
}

#[test]
fn receiver_death_and_launcher_refusal_are_distinct_paths_to_failure() {
    let mut model = Model::new([SPEC, Spec { origin: 2, ..SPEC }], true, 600);
    apply_all(
        &mut model,
        0,
        &[
            Event::Reserve,
            Event::Forward,
            RECEIVE,
            Event::NoAttemptProcesses,
        ],
    );
    assert_eq!(
        model.apply(0, Event::Resolve),
        Ok(Outcome::Phase(Phase::Failed))
    );
    assert_eq!(model.apply(0, Event::TakeStart), Err(Denied::Guard));
    apply_all(
        &mut model,
        1,
        &[Event::Reserve, Event::Forward, RECEIVE, Event::TakeStart],
    );
    assert_eq!(
        model.apply(1, Event::RefuseBeforeChild { launcher: 0 }),
        Ok(Outcome::Applied)
    );
    assert_eq!(model.attempt(1).phase, Phase::Failed);
}

#[test]
fn watchdog_deadline_is_supplied_by_caller() {
    let mut model = Model::default();
    apply_all(
        &mut model,
        0,
        &[
            Event::BeginRecycle,
            Event::Reserve,
            Event::Forward,
            RECEIVE,
            Event::TakeStart,
            Event::StartChild { launcher: 0 },
            Event::BindJob(4),
            Event::Claim(4),
            Event::Delivered {
                job: 4,
                generation: 0,
            },
            Event::Publish,
            Event::Listing {
                row: Listing::Live(4),
                at_secs: 1,
            },
            Event::Confirm(4),
            Event::Promote,
        ],
    );
    assert!(model.stop_overdue(0, 2, 1));
    assert!(!model.stop_overdue(0, 2, 2));
    apply_all(
        &mut model,
        0,
        &[Event::MarkStopEligible, Event::StopPredecessor],
    );
    assert!(!model.stop_overdue(0, 3, 1));
}

#[test]
fn exhaustive_interleavings_over_two_attempts_and_four_actors() {
    use std::collections::{HashSet, VecDeque};

    const DEPTH: usize = 13;
    let alphabet = [
        Event::Reserve,
        Event::Forward,
        RECEIVE,
        Event::Resolve,
        Event::TakeStart,
        Event::StartChild { launcher: 0 },
        Event::NoAttemptProcesses,
        Event::OldEnoughToRevoke { elapsed_secs: 600 },
        Event::BindJob(4),
        Event::Claim(4),
        Event::Claim(5),
        Event::Delivered {
            job: 4,
            generation: 0,
        },
        Event::NewConversation(1),
        Event::Publish,
        Event::Listing {
            row: Listing::Absent,
            at_secs: 1,
        },
        Event::BeginRecycle,
        Event::StopPredecessor,
    ];
    for other in [
        Spec { origin: 2, ..SPEC },
        Spec {
            target: 2,
            name: 1,
            ..SPEC
        },
    ] {
        let seed = Model::new([SPEC, other], true, 600);
        let mut queue = VecDeque::from([(seed.clone(), 0)]);
        let mut seen = HashSet::from([seed]);
        let mut covered = HashSet::new();
        while let Some((state, depth)) = queue.pop_front() {
            for id in 0..2 {
                covered.insert(state.attempt(id).phase);
                if depth == DEPTH {
                    continue;
                }
                for event in alphabet {
                    let mut next = state.clone();
                    if next.apply(id, event).is_err() {
                        assert_eq!(next, state, "refusal mutated state: {event:?}");
                        continue;
                    }
                    assert!(next.invariants_hold(), "{event:?} -> {next:?}");
                    if seen.insert(next.clone()) {
                        queue.push_back((next, depth + 1));
                    }
                }
            }
        }
        assert_eq!(covered.len(), 8);
    }
}
