//! Shared helpers: model builders and a breadth-first search over every ordering of events.
#![allow(dead_code)]
use pg_session_cutover_model::*;
use std::collections::{HashMap, VecDeque};

pub const URGENT: fn(u8) -> EventSpec = |source| EventSpec {
    class: Class::Urgent,
    source,
};
pub const ORDINARY: fn(u8) -> EventSpec = |source| EventSpec {
    class: Class::Ordinary,
    source,
};

pub fn params(events: Vec<EventSpec>, messages: u8) -> Params {
    Params::production(events, messages)
}

pub fn model(p: Params) -> Model {
    Model::new(p).expect("valid bounds")
}

/// Apply events that must all succeed, checking the invariants after each.
pub fn run(m: &mut Model, events: &[Event]) {
    for (i, e) in events.iter().enumerate() {
        m.apply(*e)
            .unwrap_or_else(|d| panic!("step {i} {e:?} denied {d:?}"));
        assert_eq!(m.violations(), vec![], "step {i} {e:?}");
    }
}

/// Advance whole ticks; every tick must be allowed.
pub fn ticks(m: &mut Model, secs: u16) {
    for _ in 0..secs / TICK_SECS {
        m.apply(Event::Tick).expect("time may pass");
    }
}

/// Switch the role on: shadow, sign-off, fence, recovery and a live GO, then activate.
pub fn activate(m: &mut Model) {
    run(
        m,
        &[
            Event::StartShadow,
            Event::SignOffShadow,
            Event::DeployFence(Fence::Deployed),
            Event::DeployRecovery,
            Event::GrantGo,
            Event::Activate,
        ],
    );
}

/// A session that is working and has armed its own wait.
pub fn arm_first(m: &mut Model) {
    run(
        m,
        &[
            Event::Nudge,
            Event::BeginTurn(0),
            Event::Arm(0),
            Event::EndTurn(0),
        ],
    );
}

pub struct Report {
    pub states: usize,
    pub transitions: usize,
    pub max_depth: usize,
    pub violation: Option<(Vec<Event>, Vec<Invariant>)>,
}

/// Visit every model reachable in at most `depth` events. Checks, on each transition:
/// a refusal leaves the model unchanged; an applied event keeps every invariant; event
/// stages and handling records never go backwards; time is never blocked with no way out.
pub fn explore(
    seed: Model,
    alphabet: Vec<Event>,
    depth: usize,
    mut visit: impl FnMut(&Model, Event, &Result<Outcome, Denied>, &Model),
) -> Report {
    let mut index: HashMap<Model, usize> = HashMap::from([(seed.clone(), 0)]);
    let mut parents: Vec<Option<(usize, Event)>> = vec![None];
    let mut queue = VecDeque::from([(seed, 0usize, 0usize)]);
    let (mut transitions, mut max_depth) = (0, 0);
    while let Some((state, id, d)) = queue.pop_front() {
        max_depth = max_depth.max(d);
        if d == depth {
            continue;
        }
        let mut progress = false;
        let mut tick_denied = false;
        for event in &alphabet {
            let mut next = state.clone();
            let result = next.apply(*event);
            transitions += 1;
            visit(&state, *event, &result, &next);
            let bad = match result {
                Err(denied) => {
                    assert_eq!(next, state, "refusal mutated the model: {event:?}");
                    tick_denied |= denied == Denied::Deadline;
                    continue;
                }
                Ok(_) => {
                    progress |= *event != Event::Tick && next != state;
                    monotone(&state, &next, *event);
                    next.violations()
                }
            };
            if !bad.is_empty() {
                let mut trace = vec![*event];
                let mut at = id;
                while let Some((parent, e)) = parents[at] {
                    trace.push(e);
                    at = parent;
                }
                trace.reverse();
                return Report {
                    states: index.len(),
                    transitions,
                    max_depth,
                    violation: Some((trace, bad)),
                };
            }
            if result.is_ok() && !index.contains_key(&next) {
                parents.push(Some((id, *event)));
                index.insert(next.clone(), parents.len() - 1);
                queue.push_back((next, parents.len() - 1, d + 1));
            }
        }
        assert!(
            !tick_denied || progress,
            "time is blocked with no way out: {state:?}"
        );
    }
    Report {
        states: index.len(),
        transitions,
        max_depth,
        violation: None,
    }
}

fn monotone(before: &Model, after: &Model, event: Event) {
    for e in 0..before.event_count() {
        assert!(
            after.stage(e) >= before.stage(e),
            "event {e} went back on {event:?}"
        );
    }
    for m in 0..before.message_count() {
        assert!(
            after.handled(m) || !before.handled(m),
            "message {m} lost its record on {event:?}"
        );
    }
}

pub fn is_gate(e: &Event) -> bool {
    matches!(
        e,
        Event::StartShadow
            | Event::SignOffShadow
            | Event::DeployFence(_)
            | Event::DeployRecovery
            | Event::GrantGo
            | Event::RevokeGo
            | Event::Activate
            | Event::Rollback
    )
}

pub fn is_recycle(e: &Event) -> bool {
    matches!(
        e,
        Event::BeginRecycle
            | Event::Confirm
            | Event::AbortRecycle
            | Event::Adopt
            | Event::StopOk
            | Event::StopFail
            | Event::FencedWaiterExit(_)
    )
}

pub fn is_message(e: &Event) -> bool {
    matches!(
        e,
        Event::SendMsg(_) | Event::ReadMsg { .. } | Event::HandleMsg { .. }
    )
}

/// The alphabet of `m` without the event groups that `skip` names.
pub fn alphabet_without(m: &Model, skip: impl Fn(&Event) -> bool) -> Vec<Event> {
    m.alphabet().into_iter().filter(|e| !skip(e)).collect()
}
