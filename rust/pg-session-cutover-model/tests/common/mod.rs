//! Helpers: a model builder and a script runner (`"Occur0 Crash"`) that checks the invariants
//! after every step.
#![allow(dead_code)]
use pg_session_cutover_model::*;

pub fn spec(class: UrgentClass) -> EventSpec {
    EventSpec { class }
}

pub fn go() -> EventSpec {
    spec(UrgentClass::GoRow)
}

/// An active role: a live GO and the switch on, the session idle.
pub fn active(events: Vec<EventSpec>) -> Model {
    let mut m = Model::new(Params::production(events)).expect("valid bounds");
    run(&mut m, "GrantGo Activate");
    m
}

fn parse(word: &str) -> Event {
    let digit = word
        .find(|c: char| c.is_ascii_digit())
        .unwrap_or(word.len());
    let n = word[digit..].parse().unwrap_or(0);
    match &word[..digit] {
        "Tick" => Event::Tick,
        "Occur" => Event::Occur(n),
        "WakeLost" => Event::WakeLost,
        "BeginTurn" => Event::BeginTurn,
        "Receive" => Event::Receive,
        "Crash" => Event::Crash,
        "Escalate" => Event::Escalate(n),
        "Alarm" => Event::Alarm(n),
        "Nudge" => Event::Nudge,
        "Recover" => Event::Recover,
        "GrantGo" => Event::GrantGo,
        "Activate" => Event::Activate,
        "Rollback" => Event::Rollback,
        "RevokeGo" => Event::RevokeGo,
        _ => panic!("unknown event {word}"),
    }
}

/// Apply the words of `script`; each must be allowed and keep every invariant.
pub fn run(m: &mut Model, script: &str) {
    for word in script.split_whitespace() {
        m.apply(parse(word))
            .unwrap_or_else(|d| panic!("{word} denied {d:?}"));
        assert_eq!(m.violations(), vec![], "after {word}");
    }
}

/// One event; a refusal must leave the model unchanged.
pub fn try_event(m: &mut Model, word: &str) -> Result<(), Denied> {
    let before = m.clone();
    let r = m.apply(parse(word));
    assert!(
        r.is_ok() || *m == before,
        "{word} refused but changed the model"
    );
    r
}

/// Is time refused because an obligation is due?
pub fn due(m: &mut Model) -> bool {
    try_event(m, "Tick") == Err(Denied::Deadline)
}

/// Advance whole ticks; each must be allowed.
pub fn ticks(m: &mut Model, secs: u16) {
    for _ in 0..secs / TICK_SECS {
        m.apply(Event::Tick).expect("time may pass");
    }
}
