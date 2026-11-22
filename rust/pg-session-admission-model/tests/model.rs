use pg_session_admission_model::*;

#[test]
fn work_is_denied_with_no_attempt_record() {
    let mut model = Model::new(7, 3, ["one", "two"]);
    let caller = Caller {
        attempt: 7,
        root: 3,
        job: 4,
        process: 12,
        generation: 0,
    };
    assert_eq!(
        model.apply(Event::PreTool {
            caller,
            gate: Gate::Healthy,
            tool: Tool::ExternalWork
        }),
        Err(Denied::Phase),
    );
}

#[derive(serde::Deserialize)]
struct Vectors {
    version: u8,
    attempt: u8,
    root: u8,
    files: Vec<String>,
    scenarios: Vec<Scenario>,
}

#[derive(serde::Deserialize)]
struct Scenario {
    name: String,
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

const CALLER: Caller = Caller {
    attempt: 7,
    root: 3,
    job: 4,
    process: 12,
    generation: 0,
};
const OTHER_JOB: Caller = Caller {
    job: 5,
    process: 13,
    ..CALLER
};
const NEW_CONVERSATION: Caller = Caller {
    generation: 1,
    ..CALLER
};

fn pre(caller: Caller, tool: Tool) -> Event {
    Event::PreTool {
        caller,
        gate: Gate::Healthy,
        tool,
    }
}

fn read(caller: Caller, file: usize, call: u8) -> Event {
    pre(
        caller,
        Tool::Read {
            file,
            call,
            whole: true,
        },
    )
}

fn post(caller: Caller, file: usize, call: u8, returned: impl Into<String>) -> Event {
    Event::PostRead {
        caller,
        gate: Gate::Healthy,
        file,
        call,
        returned: returned.into(),
        guidance_enabled: true,
    }
}

fn admission_prefix() -> [Event; 5] {
    [
        Event::Accept,
        Event::TakeStart,
        Event::BindLaunchedJob {
            job: CALLER.job,
            process: CALLER.process,
        },
        pre(
            CALLER,
            Tool::Admit {
                attempt: CALLER.attempt,
            },
        ),
        Event::Admit {
            caller: CALLER,
            gate: Gate::Healthy,
        },
    ]
}

#[test]
fn exported_review_scenarios() {
    let vectors: Vectors =
        serde_json::from_str(include_str!("../fixtures/transitions.json")).unwrap();
    assert_eq!(vectors.version, 1);
    assert_eq!(vectors.scenarios.len(), 11);
    for scenario in vectors.scenarios {
        let mut model = Model::new(vectors.attempt, vectors.root, vectors.files.clone());
        for (index, step) in scenario.steps.into_iter().enumerate() {
            let expected = match step.expect {
                Expected::Ok(outcome) => Ok(outcome),
                Expected::Err(denied) => Err(denied),
            };
            assert_eq!(
                model.apply(step.event.clone()),
                expected,
                "{} step {index}: {:?}",
                scenario.name,
                step.event
            );
            assert!(model.invariants_hold(), "{} step {index}", scenario.name);
        }
    }
}

#[test]
fn r5_task_exceeds_bash_output_limit_and_requires_all_bytes() {
    let prompt = "x".repeat(8193);
    let mut model = Model::new(7, 3, [prompt.clone()]);
    for event in admission_prefix() {
        assert!(model.apply(event).is_ok());
    }
    assert_eq!(model.apply(read(CALLER, 0, 1)), Ok(Outcome::TaskRead));
    assert_eq!(
        model.apply(post(CALLER, 0, 1, &prompt[..8192])),
        Ok(Outcome::IgnoredRead)
    );
    let receipt = Event::Receipt {
        caller: CALLER,
        gate: Gate::Healthy,
    };
    assert_eq!(model.apply(receipt.clone()), Err(Denied::Delivery));
    assert_eq!(model.apply(read(CALLER, 0, 2)), Ok(Outcome::TaskRead));
    assert_eq!(
        model.apply(post(CALLER, 0, 2, prompt)),
        Ok(Outcome::Marked { guidance: true })
    );
    assert_eq!(model.apply(receipt), Ok(Outcome::Applied));
    assert_eq!(
        model.apply(Event::Publish {
            verified_generation: Some(0)
        }),
        Ok(Outcome::Applied)
    );
}

#[test]
fn exhaustive_interleavings_preserve_admission_and_first_work() {
    use std::collections::{HashSet, VecDeque};

    const DEPTH: usize = 15;
    let mut alphabet = Vec::from(admission_prefix());
    alphabet.extend([
        Event::ObserveScope {
            job: OTHER_JOB.job,
            process: OTHER_JOB.process,
        },
        Event::Admit {
            caller: OTHER_JOB,
            gate: Gate::Healthy,
        },
        Event::Revoke,
        Event::FailUnclaimed,
        Event::FailEnded,
        Event::AdvanceConversation {
            job: CALLER.job,
            generation: 1,
        },
        Event::Publish {
            verified_generation: Some(0),
        },
        Event::Publish {
            verified_generation: Some(1),
        },
        pre(OTHER_JOB, Tool::Work),
        Event::PreTool {
            caller: CALLER,
            gate: Gate::HookFailure,
            tool: Tool::Work,
        },
    ]);
    for caller in [CALLER, NEW_CONVERSATION] {
        alphabet.extend([
            read(caller, 0, 8),
            read(caller, 1, 9),
            post(caller, 0, 8, "one"),
            post(caller, 1, 9, "two"),
            post(caller, 0, 8, "partial"),
            Event::Receipt {
                caller,
                gate: Gate::Healthy,
            },
            Event::Ready { caller },
            pre(caller, Tool::Work),
        ]);
    }

    let seed = Model::new(7, 3, ["one", "two"]);
    let mut queue = VecDeque::from([(seed.clone(), 0)]);
    let mut seen = HashSet::from([seed]);
    let mut covered = HashSet::new();
    let mut work_generations = HashSet::new();
    while let Some((state, depth)) = queue.pop_front() {
        covered.insert(state.phase());
        if depth == DEPTH {
            continue;
        }
        for event in &alphabet {
            let mut next = state.clone();
            let result = next.apply(event.clone());
            if result == Ok(Outcome::WorkAllowed) {
                let Event::PreTool { caller, gate, .. } = event else {
                    panic!("work escaped the tool gate: {event:?}");
                };
                assert_eq!(state.phase(), Phase::Published);
                assert_eq!(caller.job, CALLER.job);
                assert_eq!(caller.process, CALLER.process);
                assert_eq!(*gate, Gate::Healthy);
                work_generations.insert(caller.generation);
            }
            if result.is_err() {
                assert_eq!(next, state, "refusal mutated state: {event:?}");
            }
            assert!(next.invariants_hold(), "{event:?} -> {next:?}");
            if seen.insert(next.clone()) {
                queue.push_back((next, depth + 1));
            }
        }
    }
    assert_eq!(covered.len(), 7);
    assert_eq!(work_generations, HashSet::from([0, 1]));
}
