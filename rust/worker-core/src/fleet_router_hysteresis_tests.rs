use super::*;
fn evaluator(enter: u32) -> Evaluator {
    Evaluator::new(Config {
        schema_version: 1,
        signals: NAMES
            .iter()
            .enumerate()
            .map(|(i, n)| Rule {
                name: (*n).into(),
                unit: format!("unit{i}"),
                cadence: 10,
                high: 10. + i as f64,
                low: i as f64,
                enter,
                exit: 2,
                freshness: 10,
            })
            .collect(),
    })
    .unwrap()
}
fn snapshot(t: i64, high: bool) -> Snapshot {
    Snapshot {
        schema_version: 1,
        timestamp: t,
        start_exclusive: Some(t - 10),
        end_inclusive: t,
        signals: NAMES
            .iter()
            .enumerate()
            .map(|(i, n)| Signal {
                name: (*n).into(),
                unit: format!("unit{i}"),
                value: Reading::Known(i as f64 + if high { 10. } else { 0. }),
                observed_at: Some(t),
                provenance: vec![],
            })
            .collect(),
        events: vec![],
        pending: vec![],
    }
}
fn run(e: &mut Evaluator, t: i64, high: bool) -> Vec<Finding> {
    e.evaluate(&snapshot(t, high), t as u64, false).unwrap()
}
#[test]
fn invalid_forward_window_cannot_poison_entry_or_recovery() {
    let mut e = evaluator(9);
    let mut bad = snapshot(20, true);
    bad.end_inclusive = 10_000;
    assert!(e
        .evaluate(&bad, u64::MAX, false)
        .unwrap()
        .iter()
        .all(|f| matches!(&f.observation, Observation::Unknown(r) if r == "invalid window")));
    for t in (30..=100).step_by(10) {
        assert!(run(&mut e, t, true).is_empty());
    }
    let entered = run(&mut e, 110, true);
    assert_eq!(entered.len(), 6);
    assert!(entered
        .iter()
        .all(|f| f.observation == Observation::Overload(1)));
    bad = snapshot(120, false);
    bad.end_inclusive = 10_000;
    assert_eq!(e.evaluate(&bad, u64::MAX, false).unwrap().len(), 6);
    assert!(run(&mut e, 130, false).is_empty());
    let recovered = run(&mut e, 140, false);
    assert_eq!(recovered.len(), 6);
    assert!(recovered
        .iter()
        .all(|f| f.observation == Observation::Recovery(1)));
}
#[test]
fn exhausted_episode_is_unknown_without_dropping_other_signals() {
    let mut e = evaluator(1);
    e.states.insert(
        NAMES[1].into(),
        State {
            episode: u64::MAX,
            ..State::default()
        },
    );
    e = serde_json::from_slice(&serde_json::to_vec(&e).unwrap()).unwrap();
    let findings = run(&mut e, 10, true);
    assert_eq!(findings.len(), 6);
    for (i, f) in findings.iter().enumerate() {
        assert_eq!(
            f.observation,
            if i == 1 {
                Observation::Unknown("episode exhausted".into())
            } else {
                Observation::Overload(1)
            }
        );
    }
    assert!(!e.states[NAMES[1]].latched);
    assert_eq!(e.states[NAMES[1]].episode, u64::MAX);
    assert!(run(&mut e, 10, true).is_empty());
}
#[test]
fn partial_configuration_is_rejected_on_creation_and_restore() {
    for count in 0..NAMES.len() {
        let mut e = evaluator(2);
        e.config.signals.truncate(count);
        assert!(Evaluator::new(e.config.clone()).is_err(), "count {count}");
        e = serde_json::from_slice(&serde_json::to_vec(&e).unwrap()).unwrap();
        assert!(e.evaluate(&snapshot(10, true), 10, false).is_err());
    }
}
#[test]
fn thresholds_noise_deadband_counts_and_ninety_seconds() {
    let mut e = evaluator(9);
    for t in (10..=80).step_by(10) {
        assert!(run(&mut e, t, true).is_empty());
    }
    let findings = run(&mut e, 90, true);
    assert_eq!(findings.len(), 6);
    assert!(findings
        .iter()
        .all(|f| f.observation == Observation::Overload(1)));
    assert!(run(&mut e, 100, false).is_empty());
    let mut middle = snapshot(110, false);
    for s in &mut middle.signals {
        if let Reading::Known(v) = &mut s.value {
            *v += 5.;
        }
    }
    assert!(e.evaluate(&middle, 110, false).unwrap().is_empty());
    assert!(run(&mut e, 120, false).is_empty());
    assert_eq!(run(&mut e, 130, false).len(), 6);
    let mut e = evaluator(2);
    for t in (10..=200).step_by(10) {
        assert!(run(&mut e, t, t % 20 == 0).is_empty());
    }
    assert!(run(&mut e, 210, true)
        .iter()
        .all(|f| f.observation == Observation::Overload(1)));
}
#[test]
fn invalid_configuration() {
    for mode in 0..11 {
        let mut c = evaluator(2).config;
        match mode {
            0 => c.schema_version = 2,
            1 => c.signals.push(c.signals[0].clone()),
            2 => c.signals[0].name = "other".into(),
            3 => c.signals[0].unit.clear(),
            4 => c.signals[0].cadence = 0,
            5 => c.signals[0].freshness = 0,
            6 => c.signals[0].enter = 0,
            7 => c.signals[0].exit = 0,
            8 => c.signals[0].low = c.signals[0].high,
            9 => c.signals[0].high = f64::INFINITY,
            _ => c.signals[0].low = f64::NAN,
        }
        assert!(Evaluator::new(c).is_err(), "mode {mode}");
    }
}
#[test]
fn replay_gaps_restore_and_new_episode() {
    let mut e = evaluator(2);
    assert!(run(&mut e, 10, true).is_empty());
    assert!(run(&mut e, 10, true).is_empty());
    assert!(run(&mut e, 30, true).is_empty());
    assert_eq!(run(&mut e, 40, true).len(), 6);
    e = serde_json::from_slice(&serde_json::to_vec(&e).unwrap()).unwrap();
    assert!(run(&mut e, 40, true).is_empty());
    assert!(run(&mut e, 50, false).is_empty());
    assert!(run(&mut e, 60, false)
        .iter()
        .all(|f| f.observation == Observation::Recovery(1)));
    assert!(run(&mut e, 70, true).is_empty());
    assert_eq!(
        run(&mut e, 80, true)[0].observation,
        Observation::Overload(2)
    );
}
#[test]
fn reasoned_unknown_resets_and_isolation() {
    for mode in 0..10 {
        let mut e = evaluator(2);
        run(&mut e, 10, true);
        let mut s = snapshot(20, true);
        let reason = match mode {
            0 => {
                s.schema_version = 2;
                "schema"
            }
            1 => {
                s.signals.push(s.signals[0].clone());
                "duplicate"
            }
            2 => {
                s.signals.remove(0);
                "absent"
            }
            3 => {
                s.signals[0].value = Reading::Unknown("sample".into());
                "unknown"
            }
            4 => {
                s.signals[0].value = Reading::Known(f64::NAN);
                "nonfinite"
            }
            5 => {
                s.signals[0].unit.clear();
                "unit"
            }
            6 => {
                s.signals[0].observed_at = Some(0);
                "stale"
            }
            7 => {
                s.signals[0].observed_at = Some(21);
                "future"
            }
            8 => {
                s.signals[0].observed_at = None;
                "absent"
            }
            _ => {
                s.start_exclusive = None;
                "window"
            }
        };
        let f = e.evaluate(&s, 20, false).unwrap();
        assert!(matches!(&f[0].observation, Observation::Unknown(r) if r.contains(reason)));
        if mode != 0 && mode != 9 {
            assert_eq!(f[1].observation, Observation::Overload(1));
        }
        assert!(!run(&mut e, 30, true).iter().any(|f| f.signal == NAMES[0]));
        assert_eq!(
            run(&mut e, 40, true)[0].observation,
            Observation::Overload(1)
        );
        let mut bad = snapshot(50, false);
        bad.signals[0].value = Reading::Unknown("missing".into());
        e.evaluate(&bad, 50, false).unwrap();
        assert!(!run(&mut e, 60, false).iter().any(|f| f.signal == NAMES[0]));
        assert_eq!(
            run(&mut e, 70, false)[0].observation,
            Observation::Recovery(1)
        );
    }
}
#[test]
fn wall_jumps_are_inert_and_inputs_unchanged() {
    let fake_fs = BTreeMap::from([("synthetic/input", b"unchanged".to_vec())]);
    let before = fake_fs.clone();
    for wall in [-100, 1000] {
        let mut e = evaluator(2);
        run(&mut e, 10, true);
        let s = snapshot(wall, true);
        let bytes = serde_json::to_vec(&s).unwrap();
        for _ in 0..3 {
            let f = e.evaluate(&s, 20, true).unwrap();
            assert_eq!(f.len(), 6);
            assert!(f
                .iter()
                .all(|f| f.observation == Observation::Unknown("wall discontinuity".into())));
        }
        assert_eq!(serde_json::to_vec(&s).unwrap(), bytes);
        assert!(run(&mut e, wall.max(20) + 10, true).is_empty());
    }
    assert_eq!(fake_fs, before);
}
