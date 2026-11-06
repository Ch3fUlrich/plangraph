use super::*;
use serde_json::json;

#[test]
fn fleet_slots_retry_restore_and_unknown() {
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
    let mut mixed: Config = serde_json::from_value(json!({"tick_seconds":9,
        "rules":c.rules,"sources":sources,"retry_delay":1,
        "maximum_attempts":3,"silence_after":1000}))
    .unwrap();
    assert!(mixed.validate().is_err());
    mixed.tick_seconds = 10;
    mixed.rules.signals[0].high = 4.;
    for jitter in [0, 1, 9] {
        let mut saved = None;
        let mut sent = Vec::new();
        let mut opened = Vec::new();
        let mut diagnostics = Vec::new();
        for (i, slot) in [10, 20, 30, 30, 40].into_iter().enumerate() {
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
            tick(
                &c,
                &mut saved,
                slot + jitter,
                i as u64,
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
