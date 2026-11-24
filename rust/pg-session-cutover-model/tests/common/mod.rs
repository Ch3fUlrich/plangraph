//! Shared helpers: model builders and a breadth-first search over every ordering of events.
#![allow(dead_code)]
use pg_session_cutover_model::*;
use std::collections::{HashMap, VecDeque};

pub fn event(class: UrgentClass, source: u8) -> EventSpec {
    EventSpec { class, source }
}

/// One go-row event on `source`.
pub fn go(source: u8) -> EventSpec {
    event(UrgentClass::GoRow, source)
}

pub fn params(events: Vec<EventSpec>) -> Params {
    Params::production(events)
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
            Event::DeployFence,
            Event::DeployRecovery,
            Event::GrantGo,
            Event::Activate,
        ],
    );
}

/// Activated role whose session has armed its own wait and is idle.
pub fn armed_idle(p: Params) -> Model {
    let mut m = model(p);
    activate(&mut m);
    run(
        &mut m,
        &[Event::Nudge, Event::BeginTurn, Event::Arm, Event::EndTurn],
    );
    assert_eq!((m.life(), m.waiter()), (Life::Idle, Waiter::Armed));
    m
}

pub fn scan_all(m: &mut Model) {
    run(m, &[Event::Scan(0), Event::Scan(1)]);
}

/// Let `secs` pass, reading every source whenever a scan comes due.
pub fn pass(m: &mut Model, secs: u16) {
    for _ in 0..secs / TICK_SECS {
        if m.apply(Event::Tick) == Err(Denied::Deadline) {
            scan_all(m);
            m.apply(Event::Tick).expect("time may pass after the scans");
        }
    }
}

pub struct Report {
    pub states: usize,
    pub max_depth: usize,
    pub violation: Option<(Vec<Event>, Vec<Invariant>)>,
}

/// Visit every model reachable in at most `depth` events. Checks, on each transition: a refusal
/// leaves the model unchanged; an applied event keeps every invariant; event stages never go
/// back; time is never blocked with no way out.
pub fn explore(
    seed: Model,
    alphabet: Vec<Event>,
    depth: usize,
    mut visit: impl FnMut(&Model, Event, &Result<Outcome, Denied>, &Model),
) -> Report {
    let mut index: HashMap<Model, usize> = HashMap::from([(seed.clone(), 0)]);
    let mut parents: Vec<Option<(usize, Event)>> = vec![None];
    let mut queue = VecDeque::from([(seed, 0usize, 0usize)]);
    let mut max_depth = 0;
    while let Some((state, id, d)) = queue.pop_front() {
        max_depth = max_depth.max(d);
        if d == depth {
            continue;
        }
        let (mut progress, mut tick_denied) = (false, false);
        for event in &alphabet {
            let mut next = state.clone();
            let result = next.apply(*event);
            visit(&state, *event, &result, &next);
            let bad = match result {
                Err(denied) => {
                    assert_eq!(next, state, "refusal mutated the model: {event:?}");
                    tick_denied |= denied == Denied::Deadline;
                    continue;
                }
                Ok(_) => {
                    progress |= *event != Event::Tick && next != state;
                    for e in 0..state.event_count() {
                        assert!(
                            next.stage(e) >= state.stage(e),
                            "event {e} went back on {event:?}"
                        );
                    }
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
                    max_depth,
                    violation: Some((trace, bad)),
                };
            }
            if !index.contains_key(&next) {
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
        max_depth,
        violation: None,
    }
}

pub fn is_gate(e: &Event) -> bool {
    matches!(
        e,
        Event::StartShadow
            | Event::SignOffShadow
            | Event::DeployFence
            | Event::DeployRecovery
            | Event::GrantGo
            | Event::RevokeGo
            | Event::Activate
            | Event::Rollback
    )
}

/// The alphabet of `m` without the event groups that `skip` names.
pub fn alphabet_without(m: &Model, skip: impl Fn(&Event) -> bool) -> Vec<Event> {
    m.alphabet().into_iter().filter(|e| !skip(e)).collect()
}
