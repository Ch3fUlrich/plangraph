use super::*;
#[test]
fn zero_ids_fall_back() {
    for id in [json!(0), json!(0.0), json!(-0.0), json!(false), json!("")] {
        let result = build(&json!({"registry":[{"id":id,"host_id":"a"}]})).unwrap();
        assert_eq!(result["hosts"][0]["host_id"], "a");
    }
}
fn project(samples: Value, runs: Value) -> Value {
    build(
        &json!({"registry":[{"id":"a","aliases":["alias"],"class":"worker",
        "caps":{"max_runs":4}},{"id":"b"}], "samples":samples,"runs":runs}),
    )
    .unwrap()
}
#[test]
fn samples_and_metrics() {
    let values = [
        json!(12.5),
        json!(1024),
        json!(0),
        json!(20),
        json!(30),
        json!(4096),
    ];
    let sample: serde_json::Map<String, Value> = METRICS
        .iter()
        .zip(values)
        .map(|(k, v)| (k.to_string(), v))
        .collect();
    let result = project(json!([["alias", sample], ["stranger", sample]]), json!([]));
    assert_eq!(result["hosts"][0]["sample"], json!(sample));
    assert_eq!(result["hosts"][0]["class"], "worker");
    assert_eq!(result["hosts"][0]["caps"], json!({"max_runs":4}));
    assert_eq!(result["hosts"][1]["host_id"], "b");
    assert_eq!(result["hosts"][1]["samples"], "unavailable");
    assert_eq!(result["running_agents"], 0);
    for v in [Value::Null, json!("unreadable"), json!(true), json!(-1)] {
        for key in METRICS {
            assert!(metric(&v, key).is_null());
        }
    }
    assert!(metric(&json!(101), "cpu_percent").is_null());
    assert_eq!(metric(&json!(0), "gpu_percent"), 0);
    assert!(metric(&json!(f64::NAN), "cpu_percent").is_null());
    assert!(metric(&json!(f64::INFINITY), "cpu_percent").is_null());
}
#[test]
fn counts_and_unknown() {
    let runs = json!([{"host":"alias","run_id":"one","status":"running"},
        {"host":"a","run_id":"one","status":"running"},
        {"host":"b","run_id":"two","status":"running"},
        {"host":"a","run_id":"three","status":"completed"},
        {"host":"a","run_id":"four","status":"queued"},
        {"host":"a","run_id":"five","status":"failed"}]);
    assert_eq!(project(json!([]), runs.clone())["running_agents"], 2);
    for runs in [
        Value::Null,
        json!([{}]),
        json!([{"host":"unknown","run_id":"x","status":"running"}]),
        json!([runs[0],{"host":"a","run_id":"one","status":"completed"}]),
    ] {
        let result = project(json!([]), runs);
        assert!(result["running_agents"].is_null());
        assert!(result["hosts"][0]["running_agents"].is_null());
    }
    assert_eq!(
        build(&json!({"registry":[],"runs":[]})).unwrap(),
        json!({"hosts":[],"running_agents":0})
    );
}
#[test]
fn aliases_refused() {
    for registry in [
        json!([{"id":"a","aliases":["shared"]},{"id":"b","aliases":["shared"]}]),
        json!([{"id":"a","aliases":["b"]},{"id":"b"}]),
        json!([{"id":"a"},{"id":"b","aliases":["a"]}]),
    ] {
        assert_eq!(
            build(&json!({"registry":registry})).unwrap_err(),
            "ambiguous registry alias"
        );
    }
    for aliases in [
        json!("abc"),
        Value::Null,
        json!({}),
        json!(1),
        json!([1]),
        json!([null]),
    ] {
        assert!(build(&json!({"registry":[{"id":"a","aliases":aliases}]})).is_err());
    }
}
