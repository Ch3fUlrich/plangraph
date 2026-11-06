use super::*;
use serde_json::json;

#[test]
fn fleet_slots_retry_restore_and_unknown() {
    let (_d, c) = setup();
    let sources = c.sources.clone();
    let mut mixed: Config = serde_json::from_value(json!({"tick_seconds":9,
        "rules":c.rules,"sources":sources,"retry_delay":1,
        "maximum_attempts":3,"silence_after":1000}))
    .unwrap();
    assert!(mixed.validate().is_err());
    mixed.tick_seconds = 10;
    mixed.rules.signals[0].high = 4.;
    for jittered in [false, true] {
        let mut saved = None;
        let mut sent = Vec::new();
        let mut opened = Vec::new();
        let mut diagnostics = Vec::new();
        for (i, slot) in [10, 20, 30, 30, 40].into_iter().enumerate() {
            let jitter = if jittered { [1, 9, 1, 2, 9][i] } else { 0 };
            for path in &sources {
                fs::write(
                    path,
                    json!({"schema_version":1,"observed_at":slot,
                    "complete":true,"events":[{"kind":"ctx","tokens":3,
                    "source":"sample","event_id":"sample","timestamp":slot}]})
                    .to_string(),
                )
                .unwrap();
            }
            let acks = if i == 4 {
                BTreeSet::from(["hysteresis:rolling_ctx:1".into()])
            } else {
                BTreeSet::new()
            };
            let before = serde_json::to_value(&saved).unwrap();
            let result = tick(
                &c,
                &mut saved,
                slot + jitter,
                (slot + jitter + i64::from(i == 3 && !jittered)) as u64,
                false,
                &acks,
                None,
                &mut |id| {
                    sent.push(id.to_owned());
                    Ok(())
                },
                &mut opened,
                &mut diagnostics,
            )
            .unwrap();
            if i == 3 {
                assert!(result.is_empty());
                let after = serde_json::to_value(&saved).unwrap();
                assert_eq!(before["evaluator"], after["evaluator"]);
                assert_eq!(before["mono"], after["mono"]);
            }
            saved = serde_json::from_slice(&serde_json::to_vec(&saved).unwrap()).unwrap();
            assert_eq!(opened.len(), usize::from(i >= 2));
        }
        assert_eq!(sent, ["hysteresis:rolling_ctx:1"; 2]);
        let before = serde_json::to_value(&saved).unwrap();
        tick(
            &mixed,
            &mut saved,
            50,
            5,
            false,
            &BTreeSet::new(),
            None,
            &mut |_| Ok(()),
            &mut opened,
            &mut diagnostics,
        )
        .unwrap();
        assert_eq!(diagnostics, ["hysteresis rules changed"]);
        let after = serde_json::to_value(&saved).unwrap();
        assert_eq!(before["evaluator"], after["evaluator"]);
        assert_eq!(before["rules"], after["rules"]);
        assert_eq!(before["slot"], after["slot"]);
        fs::remove_file(&sources[0]).unwrap();
        let findings = tick(
            &c,
            &mut saved,
            50,
            5,
            false,
            &BTreeSet::new(),
            None,
            &mut |_| Ok(()),
            &mut opened,
            &mut diagnostics,
        )
        .unwrap();
        assert!(findings
            .iter()
            .all(|f| matches!(f.observation, router::Observation::Unknown(_))));
    }
}

fn setup() -> (tempfile::TempDir, Config) {
    let d = tempfile::tempdir().unwrap();
    let sources = ["events", "asks", "claims"].map(|n| d.path().join(n));
    let units = [
        "tokens",
        "events",
        "asks",
        "IDs/second",
        "claims",
        "seconds",
    ];
    let rules = router::NAMES
        .iter()
        .zip(units)
        .map(|(name, unit)| {
            json!({"name":name,"unit":unit,"cadence":10,"high":2,"low":1,
            "enter":3,"exit":2,"freshness":10})
        })
        .collect::<Vec<_>>();
    let c: Config = serde_json::from_value(json!({"tick_seconds":10,
        "rules":{"schema_version":1,"signals":rules},"sources":sources,
        "retry_delay":1,"maximum_attempts":3,"silence_after":1000}))
    .unwrap();
    c.validate().unwrap();
    (d, c)
}

#[test]
fn fleet_hysteresis_noisy_and_gap() {
    let (_d, c) = setup();
    for samples in [
        vec![(10, 3), (20, 0), (30, 3), (40, 0), (50, 3)],
        vec![(10, 3), (20, 3), (40, 3), (50, 3), (60, 3)],
    ] {
        let mut saved = None;
        let mut opened = Vec::new();
        for (wall, tokens) in &samples {
            for path in &c.sources {
                fs::write(
                    path,
                    json!({"schema_version":1,"observed_at":wall,
                    "complete":true,"events":[{"kind":"ctx","tokens":tokens,
                    "source":"sample","event_id":"sample","timestamp":wall}]})
                    .to_string(),
                )
                .unwrap();
            }
            tick(
                &c,
                &mut saved,
                *wall,
                *wall as u64,
                false,
                &BTreeSet::new(),
                None,
                &mut |_| Ok(()),
                &mut opened,
                &mut Vec::new(),
            )
            .unwrap();
            if *wall < 60 {
                assert!(opened.is_empty(), "premature alarm at {wall}");
            }
        }
        assert_eq!(opened.len(), usize::from(samples.last().unwrap().0 == 60));
    }
}

#[test]
fn fleet_hysteresis_error_ticks_delivery_and_clock() {
    let (_d, c) = setup();
    let mut saved = None;
    let mut opened = Vec::new();
    let mut diagnostics = Vec::new();
    let mut sent = Vec::new();
    let mut run = |saved: &mut Option<State>, wall| {
        tick(
            &c,
            saved,
            wall,
            wall as u64,
            false,
            &BTreeSet::new(),
            None,
            &mut |id| {
                sent.push(id.to_owned());
                Ok(())
            },
            &mut opened,
            &mut diagnostics,
        )
    };
    run(&mut saved, 10).unwrap();
    let mut value = serde_json::to_value(&saved).unwrap();
    value["active"] = json!(["hysteresis:rolling_ctx:1"]);
    value["evaluator"]["states"]["unexpected"] = json!({
        "last":null,"entering":0,"exiting":0,"episode":0,"latched":false});
    saved = serde_json::from_value(value).unwrap();
    run(&mut saved, 20).unwrap();
    assert_eq!(saved.as_ref().unwrap().slot, Some(2));
    assert_eq!(saved.as_ref().unwrap().mono, 10);
    let mut value = serde_json::to_value(&saved).unwrap();
    value["evaluator"]["states"]
        .as_object_mut()
        .unwrap()
        .remove("unexpected");
    saved = serde_json::from_value(value).unwrap();
    run(&mut saved, 30).unwrap();
    assert_eq!(saved.as_ref().unwrap().mono, 20);
    assert_eq!(sent, ["hysteresis:rolling_ctx:1"]);
    assert!(saved.as_ref().unwrap().active.is_empty());
    assert_eq!(diagnostics, ["unconfigured state"]);
}

#[test]
fn fleet_hysteresis_ack_prunes_active() {
    let (_d, c) = setup();
    let mut saved = None;
    tick(
        &c,
        &mut saved,
        10,
        10,
        false,
        &BTreeSet::new(),
        None,
        &mut |_| Ok(()),
        &mut Vec::new(),
        &mut Vec::new(),
    )
    .unwrap();
    saved
        .as_mut()
        .unwrap()
        .active
        .insert("hysteresis:rolling_ctx:1".into());
    tick(
        &c,
        &mut saved,
        10,
        11,
        false,
        &BTreeSet::from(["hysteresis:rolling_ctx:1".into()]),
        None,
        &mut |_| Ok(()),
        &mut Vec::new(),
        &mut Vec::new(),
    )
    .unwrap();
    assert!(saved.unwrap().active.is_empty());
}
