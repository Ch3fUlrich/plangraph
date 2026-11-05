use super::*;
fn config() -> Config {
    Config {
        maximum_age: 10,
        divergence_tolerance: 2,
        compaction_cap: 3,
        silence_deadline: 20,
        retry_delay: 1,
        maximum_attempts: 2,
    }
}
fn pair(a: i64, b: i64, count: u64) -> Snapshot {
    Snapshot {
        members: [a, b].map(|t| Member {
            identity: String::new(),
            revision: "r1".into(),
            checkpoint: "c1".into(),
            freshness: Some(t),
            provenance: "fake/source".into(),
        }),
        compactions: Some(count),
    }
}
fn bytes(mut s: Snapshot) -> Vec<u8> {
    s.members[0].identity = "synthetic-a".into();
    s.members[1].identity = "synthetic-b".into();
    serde_json::to_vec(&s).unwrap()
}
fn run(state: &mut State, s: Snapshot, t: u64) -> Finding {
    state.observe(
        &config(),
        &bytes(s),
        Clock {
            wall: 100 + t as i64,
            monotonic: t,
            discontinuity: None,
        },
    )
}
#[test]
fn zero_compaction_age_boundary() {
    let mut state = State::default();
    assert!(!run(&mut state, pair(100, 100, 0), 0).new_alarm);
    assert_eq!(run(&mut state, pair(100, 100, 0), 9).ages, [Some(9); 2]);
    let f = run(&mut state, pair(100, 100, 0), 10);
    assert!(f.new_alarm);
    assert_eq!(f.reasons, ["stale"]);
    assert_eq!(f.elapsed_compactions, Some(0));
    assert_eq!(f.evidence.unwrap().members[0].freshness, Some(100));
    let f = run(&mut state, pair(100, 100, 0), 11);
    assert_eq!(f.ages, [Some(11); 2]);
    assert!(!f.new_alarm);
    assert_eq!(f.episode, Some(1));
}
#[test]
fn coherent_boundaries_rearm() {
    let mut state = State::default();
    run(&mut state, pair(100, 100, 0), 0);
    let f = run(&mut state, pair(102, 100, 2), 2);
    assert!(f.reasons.is_empty());
    assert_eq!(f.elapsed_compactions, Some(2));
    let mut mismatch = pair(102, 100, 3);
    mismatch.members[1].revision = "other".into();
    let f = run(&mut state, mismatch, 3);
    assert_eq!(f.reasons, ["compaction cap"]);
    assert!(f.new_alarm);
    let f = run(&mut state, pair(110, 100, 4), 10);
    assert_eq!(f.reasons, ["stale", "divergence", "compaction cap"]);
    assert_eq!(f.episode, Some(1));
    assert!(!f.new_alarm);
    assert_eq!(run(&mut state, pair(111, 111, 5), 11).episode, None);
    assert_eq!(run(&mut state, pair(111, 111, 5), 21).episode, Some(2));
}
#[test]
fn clock_faults_unknown_and_immutable_source() {
    for (wall, reason) in [(50, "backward"), (1000, "forward")] {
        let mut state = State::default();
        let source = bytes(pair(100, 100, 0));
        let original = source.clone();
        run(&mut state, pair(100, 100, 0), 0);
        let f = state.observe(
            &config(),
            &source,
            Clock {
                wall,
                monotonic: 10,
                discontinuity: Some(reason.into()),
            },
        );
        assert!(f.new_alarm);
        assert_eq!(f.ages, [Some(10); 2]);
        assert!(!f.diagnostics.is_empty());
        assert_eq!(source, original);
        let f = run(&mut state, pair(110, 110, 0), 9);
        assert_eq!(f.ages, [None; 2]);
        assert_eq!(f.episode, Some(1));
        assert!(!f.new_alarm);
        assert!(f.diagnostics.contains(&"monotonic regression".into()));
    }
    let mut state = State::default();
    run(&mut state, pair(100, 100, 4), 0);
    let f = run(&mut state, pair(101, 101, 3), 1);
    assert_eq!(f.elapsed_compactions, None);
    assert!(!f.diagnostics.is_empty());
    let mut missing = pair(200, 100, 4);
    missing.members[1].freshness = None;
    let f = run(&mut state, missing, 11);
    assert!(f.new_alarm);
    assert_eq!(f.ages, [Some(10), Some(10)]);
    assert!(!f.diagnostics.is_empty());
    let f = state.observe(
        &config(),
        b"{",
        Clock {
            wall: 112,
            monotonic: 12,
            discontinuity: None,
        },
    );
    assert!(f.evidence.is_none());
    assert!(f.reasons.contains(&"stale".into()));
}
#[test]
fn dark_json_has_no_actions() {
    let f = run(&mut State::default(), pair(90, 90, 0), 0);
    let json = serde_json::to_value(f).unwrap();
    assert!(json["new_alarm"].as_bool().unwrap());
    for forbidden in ["stop", "recycle", "pause", "timer", "delivery"] {
        assert!(json.get(forbidden).is_none());
    }
    let lib = include_str!("lib.rs");
    assert_eq!(lib.matches("pub mod fleet_stale_pair_alert;").count(), 1);
}
