use super::*;
use serde_json::json;
const SHA: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
fn enable() -> Activation {
    Activation {
        name: "ENABLE-FLEET-CAP-RESOLVER".into(),
        sha: SHA.into(),
        decision_run: "synthetic-decision/run".into(),
        at_s: 90,
        reviewed_rollback: "synthetic-reviewed-rollback".into(),
    }
}
fn policy(numerator: u64, denominator: u64, unknown: UnknownPolicy) -> Policy {
    Policy {
        numerator,
        denominator,
        unknown,
    }
}
fn id() -> Value {
    json!({"role":"synthetic","model":"small","harness":"fixture"})
}
fn catalog(n: u64) -> Value {
    json!({"schema_version":1,"observed_at":90,"entries":[{"identity":id(),
        "base":{"window":{"Known":n},"unit":"tokens","observed_at":90,
        "expires_at":110,"provenance":"synthetic-base"}}]})
}
fn run(v: &Value, prior: Option<u64>, p: Option<&Policy>, a: Option<&Activation>) -> Resolution {
    resolve(
        &id(),
        Ok(&serde_json::to_vec(v).unwrap()),
        100,
        SHA,
        prior,
        p,
        a,
    )
}
fn unknown(out: &Resolution) {
    assert!(matches!(out.window, Window::Unknown(_)));
    assert!(!out.alarms.is_empty());
}
#[test]
fn default_pass_through() {
    let p = policy(1, 1, UnknownPolicy::CapAtConfiguredAndAlarm(3));
    for prior in [Some(7), Some(900_000), None] {
        for v in [catalog(8), catalog(900_001), Value::Null] {
            assert_eq!(run(&v, prior, Some(&p), None).threshold, prior);
        }
        for bytes in [Ok(&b"{"[..]), Err("missing")] {
            assert_eq!(
                resolve(&id(), bytes, 100, SHA, prior, Some(&p), None).threshold,
                prior
            );
        }
    }
}
#[test]
fn activation_gate() {
    let p = policy(1, 1, UnknownPolicy::KeepPriorAndAlarm);
    assert_eq!(
        run(&catalog(8), Some(7), Some(&p), Some(&enable())).threshold,
        Some(8)
    );
    for field in [
        "name",
        "foundation",
        "sha",
        "future",
        "decision",
        "rollback",
        "empty_sha",
    ] {
        let mut a = enable();
        match field {
            "name" => a.name = "wrong".into(),
            "foundation" => a.name = "ENABLE-FLEET-FOUNDATION".into(),
            "sha" => a.sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into(),
            "future" => a.at_s = 101,
            "decision" => a.decision_run.clear(),
            "rollback" => a.reviewed_rollback.clear(),
            _ => a.sha.clear(),
        }
        let out = run(&catalog(8), Some(7), Some(&p), Some(&a));
        assert_eq!(out.threshold, Some(7), "{field}");
        unknown(&out);
    }
}
#[test]
fn identities_overrides_and_duplicates() {
    let p = policy(1, 1, UnknownPolicy::KeepPriorAndAlarm);
    for (model, n) in [("small", 17), ("large", 900_001)] {
        let mut v = catalog(n);
        let mut identity = id();
        identity["model"] = json!(model);
        v["entries"][0]["identity"] = identity.clone();
        let bytes = serde_json::to_vec(&v).unwrap();
        assert_eq!(
            resolve(
                &identity,
                Ok(&bytes),
                100,
                SHA,
                None,
                Some(&p),
                Some(&enable())
            )
            .threshold,
            Some(n)
        );
    }
    let mut v = catalog(17);
    v["entries"][0]["override"] = v["entries"][0]["base"].clone();
    v["entries"][0]["override"]["window"] = json!({"Known":23});
    let out = run(&v, None, Some(&p), Some(&enable()));
    assert_eq!(out.threshold, Some(23));
    assert_eq!(out.source, "override");
    assert_eq!(out.provenance["provenance"], "synthetic-base");
    for bad in [json!(null), json!({"window":{"Known":99}})] {
        v["entries"][0]["override"] = bad;
        unknown(&run(&v, None, Some(&p), Some(&enable())));
    }
    v = catalog(17);
    v["entries"][0]["identity"]["model"] = json!("small-alias");
    unknown(&run(&v, None, Some(&p), Some(&enable())));
    v = catalog(17);
    let mut other = v["entries"][0].clone();
    other["base"]["window"] = json!({"Known":18});
    v["entries"].as_array_mut().unwrap().push(other);
    unknown(&run(&v, None, Some(&p), Some(&enable())));
}
#[test]
fn catalog_faults_revalidated() {
    let p = policy(1, 1, UnknownPolicy::KeepPriorAndAlarm);
    for (pointer, bad) in [
        ("/schema_version", json!(2)),
        ("/observed_at", json!(101)),
        ("/entries/0/base/expires_at", json!(100)),
        ("/entries/0/base/observed_at", json!(101)),
        ("/entries/0/base/window", json!({"Unknown":"unmeasured"})),
    ] {
        let mut v = catalog(17);
        *v.pointer_mut(pointer).unwrap() = bad;
        let out = run(&v, None, Some(&p), Some(&enable()));
        unknown(&out);
        assert_eq!(out.threshold, None);
    }
    let mut v = catalog(17);
    v["entries"][0]["override"] = v["entries"][0]["base"].clone();
    v["entries"][0]["override"]["expires_at"] = json!(100);
    unknown(&run(&v, None, Some(&p), Some(&enable())));
    let capture = catalog::decode(&serde_json::to_vec(&catalog(17)).unwrap(), 105);
    let out = run(
        &serde_json::to_value(capture).unwrap(),
        None,
        Some(&p),
        Some(&enable()),
    );
    unknown(&out);
    assert!(out
        .catalog
        .unwrap()
        .findings
        .iter()
        .any(|f| f.reason == "clock_backwards"));
    for bytes in [Err("missing_file"), Ok(&b"{"[..])] {
        let out = resolve(&id(), bytes, 100, SHA, None, Some(&p), Some(&enable()));
        unknown(&out);
        assert_eq!(out.threshold, None);
        assert!(!out.catalog.unwrap().findings.is_empty());
    }
}
#[test]
fn policies_and_arithmetic() {
    let a = enable();
    let keep = policy(2, 3, UnknownPolicy::KeepPriorAndAlarm);
    assert_eq!(
        run(&catalog(17), None, Some(&keep), Some(&a)).threshold,
        Some(11)
    );
    for (n, num, den) in [
        (17, 0, 1),
        (17, 1, 0),
        (17, 2, 1),
        (1, 1, 2),
        (u64::MAX, 2, 2),
    ] {
        let p = policy(num, den, UnknownPolicy::KeepPriorAndAlarm);
        let out = run(&catalog(n), None, Some(&p), Some(&a));
        unknown(&out);
        assert_eq!(out.threshold, None);
    }
    for prior in [None, Some(3), Some(20)] {
        let out = run(&Value::Null, prior, Some(&keep), Some(&a));
        unknown(&out);
        assert_eq!(out.threshold, prior);
        if prior.is_none() {
            assert!(out
                .alarms
                .iter()
                .any(|f| f.reason == "unresolved_no_threshold"));
        }
        let cap = policy(1, 1, UnknownPolicy::CapAtConfiguredAndAlarm(10));
        let out = run(&Value::Null, prior, Some(&cap), Some(&a));
        unknown(&out);
        assert_eq!(out.threshold, Some(prior.unwrap_or(10).min(10)));
        assert_eq!(out.source, "policy:CapAtConfiguredAndAlarm");
        let zero = policy(1, 1, UnknownPolicy::CapAtConfiguredAndAlarm(0));
        for p in [None, Some(&zero)] {
            let out = run(&catalog(17), prior, p, Some(&a));
            unknown(&out);
            assert_eq!(out.threshold, prior);
        }
    }
}
#[test]
fn consumer_parity_json_and_immutable_inputs() {
    let fake_fs = std::collections::BTreeMap::from([(
        "injected-catalog",
        serde_json::to_vec(&catalog(17)).unwrap(),
    )]);
    let before = fake_fs.clone();
    let p = policy(1, 1, UnknownPolicy::KeepPriorAndAlarm);
    let mut results = vec![];
    for _consumer in ["guard", "watchdog", "routerctl", "digest"] {
        let out = resolve(
            &id(),
            Ok(&fake_fs["injected-catalog"]),
            100,
            SHA,
            Some(7),
            Some(&p),
            Some(&enable()),
        );
        let bytes = serde_json::to_vec(&out).unwrap();
        let restored: Resolution = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            serde_json::to_value(restored).unwrap(),
            serde_json::to_value(&out).unwrap()
        );
        assert_eq!(out.threshold, Some(17));
        results.push(bytes);
    }
    assert!(results.windows(2).all(|pair| pair[0] == pair[1]));
    assert_eq!(fake_fs, before);
}
