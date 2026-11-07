use super::*;
use crate::fleet_foundation_tick::{invoke, save_then_requests};
fn array(v: &Value) -> &Vec<Value> {
    v.as_array().unwrap()
}
fn contains(v: &Value, text: &str) -> bool {
    v.to_string().contains(text)
}
fn setup() -> (tempfile::TempDir, Value) {
    let d = tempfile::tempdir().unwrap();
    let identity = json!({"role":"judge","model":"synthetic","harness":"fake"});
    let proof = json!({"id":"proof","observed_at":0,"provenance":"fixture"});
    let catalog = json!({"schema_version":1,"observed_at":0,"entries":[{ "identity":identity,"base":{"window":{"Known":100},"unit":"tokens", "observed_at":0,"expires_at":999999,"provenance":"catalog"}, "served":[{"model":"synthetic","protocol":"json","family":"outside", "proving_run":proof}]}]});
    fs::write(d.path().join("catalog"), catalog.to_string()).unwrap();
    let c = json!({"schema_version":1,"enabled":true,"seat_supply":true, "state_path":d.path().join("state"),"outbox_path":d.path().join("outbox"), "acks_path":d.path().join("acks"),"wall_jump_tolerance_s":10, "seat_supply_config":{"policy":{"quorum":1,"sha":"a".repeat(40), "reserved_gpt_family":"reserved","writer_families":[],"disallowed_pairs":[], "mandatory_no_tools_header":null,"judge_only":false, "judge_forbidden_writer_families":[]},"catalog_path":d.path().join("catalog"), "seats":[{"identity":identity,"model":"synthetic","protocol":"json", "canonical":"one","route_family":"outside","route_observed_at":0, "route_expires_at":999999,"route_provenance":"route","catalog_provenance":"catalog", "proving_run":proof,"claimed_family":null,"witnessed_family":null,"header":[], "mandatory_no_tools_header":null,"judge_only":false, "judge_forbidden_writer_families":[],"unavailable":false, "probe":{"successful":false,"void":false,"timeout":false,"authorization_reference":""}}], "limits":{"daily_attempts":1,"timeout_s":5,"retry_cap":0}, "retry_delay":1,"maximum_attempts":2,"silence_after":10}});
    (d, c)
}
fn run(d: &tempfile::TempDir, c: &Value, t: u64) -> Result<Value, String> {
    let p = d.path().join("config");
    fs::write(&p, c.to_string()).unwrap();
    invoke(&["tick".into()], Some(p), t * 1000, t as i64)
}
fn state(d: &tempfile::TempDir) -> Value {
    serde_json::from_slice(&fs::read(d.path().join("state")).unwrap()).unwrap()
}
fn authority(c: &mut Value) {
    c["seat_supply_config"]["authority"] = json!({"reference":"approval","day":0, "budget":1,"timeout_s":5,"retry_cap":0,"existing_probe_access":["one"], "may_record_not_needed":true});
}
fn lines(d: &tempfile::TempDir, kind: &str) -> Vec<Value> {
    fs::read_to_string(d.path().join("outbox"))
        .unwrap_or_default()
        .lines()
        .map(|l| serde_json::from_str::<Value>(l).unwrap())
        .filter(|l| l["kind"] == kind)
        .collect()
}

#[test]
fn fleet_seats_flag_and_legacy() {
    let (d, mut c) = setup();
    c["seat_supply"] = json!(false);
    c.as_object_mut().unwrap().remove("seat_supply_config");
    fs::remove_file(d.path().join("catalog")).unwrap();
    run(&d, &c, 0).unwrap();
    assert!(state(&d).get("seat_supply").is_none());
    run(&d, &c, 1).unwrap();
    assert!(state(&d).get("seat_supply").is_none());
    fs::remove_file(d.path().join("state")).unwrap();
    fs::remove_file(d.path().join("state.lock")).unwrap();
    fs::remove_file(d.path().join("outbox")).unwrap();
    c["seat_supply"] = json!(true);
    assert!(run(&d, &c, 2).is_err());
    for p in ["state", "state.lock", "outbox"] {
        assert!(!d.path().join(p).exists());
    }
}
#[test]
fn fleet_seats_quorum_restore() {
    let (d, mut c) = setup();
    let first = run(&d, &c, 0).unwrap();
    assert_eq!(array(&first["alarms_opened"]).len(), 1);
    let ruling = json!({"decision_reference":"decision","sha":"a".repeat(40),"reason":"review"});
    c["seat_supply_config"]["ruling"] = ruling.clone();
    let ruled = run(&d, &c, 1).unwrap();
    assert_eq!(ruled["seat_supply"]["count"]["ruling"], ruling);
    assert_eq!(ruled["seat_supply"]["count"]["hold"], true);
    assert_eq!(state(&d)["seat_supply"]["active"], first["alarms_opened"]);
    c["seat_supply_config"]["seats"][0]["probe"]["successful"] = json!(true);
    let next = run(&d, &c, 2).unwrap();
    assert_eq!(next["seat_supply"]["count"]["hold"], false);
    assert_eq!(next["seat_supply"]["count"]["families"], json!(["outside"]));
    assert!(array(&next["alarms_opened"]).is_empty());
}
#[test]
fn fleet_seats_delivery_retry_ack_clear() {
    let (d, mut c) = setup();
    for t in 0..4 {
        run(&d, &c, t).unwrap();
    }
    let alarms = lines(&d, "alarm");
    assert_eq!(alarms.len(), 2);
    assert_eq!(alarms[0]["id"], alarms[1]["id"]);
    fs::write(d.path().join("acks"), alarms[0]["id"].as_str().unwrap()).unwrap();
    run(&d, &c, 4).unwrap();
    assert_eq!(state(&d)["seat_supply"]["active"], json!([]));
    run(&d, &c, 86400).unwrap();
    assert_eq!(array(&state(&d)["seat_supply"]["active"]).len(), 1);
    c["seat_supply_config"]["seats"][0]["probe"]["successful"] = json!(true);
    run(&d, &c, 86401).unwrap();
    assert_eq!(state(&d)["seat_supply"]["active"], json!([]));
}
#[test]
fn fleet_seats_midnight_hold() {
    let (d, mut c) = setup();
    let first = run(&d, &c, 86399).unwrap()["alarms_opened"][0].clone();
    let second = run(&d, &c, 86400).unwrap()["alarms_opened"][0].clone();
    assert_eq!(state(&d)["seat_supply"]["active"], json!([first, second]));
    let delivered = lines(&d, "alarm");
    assert_eq!(delivered.iter().filter(|l| l["id"] == first).count(), 2);
    assert_eq!(delivered.iter().filter(|l| l["id"] == second).count(), 1);
    c["seat_supply_config"]["seats"][0]["probe"]["successful"] = json!(true);
    run(&d, &c, 86401).unwrap();
    assert_eq!(state(&d)["seat_supply"]["active"], json!([]));
}
#[test]
fn fleet_seats_authority_records() {
    let (d, mut c) = setup();
    let records = run(&d, &c, 0).unwrap()["seat_supply"]["records"].clone();
    assert!(contains(&records, "RequiresOperatorAuthorization"));
    assert!(lines(&d, "probe_request").is_empty());
    authority(&mut c);
    c["seat_supply_config"]["seats"][0]["probe"]["successful"] = json!(true);
    let r = run(&d, &c, 1).unwrap()["seat_supply"]["records"].to_string();
    assert!(r.contains("NotNeeded") && r.contains("approval") && r.contains("quorum"));
    assert!(lines(&d, "probe_request").is_empty());
}
#[test]
fn fleet_seats_request_save_and_failed_append() {
    let (d, mut c) = setup();
    authority(&mut c);
    fs::create_dir(d.path().join("outbox")).unwrap();
    assert!(!array(&run(&d, &c, 0).unwrap()["diagnostics"]).is_empty());
    assert_eq!(
        array(&state(&d)["seat_supply"]["probe"]["attempts"]).len(),
        1
    );
    fs::remove_dir(d.path().join("outbox")).unwrap();
    run(&d, &c, 1).unwrap();
    assert!(lines(&d, "probe_request").is_empty());
    let (d, mut c) = setup();
    authority(&mut c);
    let cfg: Config = serde_json::from_value(c["seat_supply_config"].clone()).unwrap();
    let ids = BTreeSet::new();
    let (_, requests) = tick(
        &cfg,
        &mut None,
        0,
        0,
        &ids,
        None,
        &mut |_| Ok(()),
        &mut vec![],
    )
    .unwrap();
    assert_eq!(requests.len(), 1);
    let error = save_then_requests(
        || Err("state rename".into()),
        &d.path().join("outbox"),
        requests,
        &mut Vec::new(),
    );
    assert_eq!(error, Err("state rename".into()));
    assert!(lines(&d, "probe_request").is_empty());
    run(&d, &c, 1).unwrap();
    assert_eq!(lines(&d, "probe_request").len(), 1);
}
#[test]
fn fleet_seats_catalog_and_control_error() {
    let (d, mut c) = setup();
    c["stale_pair"] = json!(true);
    c["pairs"] = json!({"config":{"maximum_age":10,"divergence_tolerance":5, "compaction_cap":10,"silence_deadline":20,"retry_delay":5,"maximum_attempts":3}, "groups":[{"group":"alpha","snapshot_path":d.path().join("missing")}]});
    let units = "tokens events asks IDs/second claims seconds".split_whitespace();
    let rules = crate::fleet_router_hysteresis::NAMES
        .iter()
        .zip(units)
        .map(|(name, unit)| {
            json!({"name":name,"unit":unit,"cadence":10, "high":2,"low":1,"enter":3,"exit":2,"freshness":10})
        })
        .collect::<Vec<_>>();
    c["hysteresis"] = json!(true);
    c["hysteresis_config"] = json!({"tick_seconds":10,"rules":{"schema_version":1, "signals":rules},"sources":[d.path().join("missing"),d.path().join("missing"), d.path().join("missing")],"retry_delay":1,"maximum_attempts":1,"silence_after":100});
    fs::remove_file(d.path().join("catalog")).unwrap();
    let r = run(&d, &c, 0).unwrap();
    assert!(contains(&r["seat_supply"]["count"]["findings"], "catalog:"));
    let mut s = state(&d);
    s["seat_supply"]["counted"]["monotonic_ms"] = json!(999999);
    fs::write(d.path().join("state"), s.to_string()).unwrap();
    let r = run(&d, &c, 10).unwrap();
    assert!(contains(&r["diagnostics"], "seat control clock backwards"));
    assert_eq!(state(&d)["seat_supply"], s["seat_supply"]);
    assert_eq!(state(&d)["clock"]["total_ms"], 10000);
    assert!(state(&d)["pairs"]["alpha"].is_object());
    assert_eq!(state(&d)["hysteresis"]["slot"], 1);
    assert_eq!(array(&r["observations"]).len(), 6);
}
#[test]
fn fleet_seats_heartbeat_rearms() {
    let (d, mut c) = setup();
    c["seat_supply_config"]["maximum_attempts"] = json!(1);
    for t in [0, 10, 11] {
        run(&d, &c, t).unwrap();
    }
    let prefix = "seat-supply:silence:";
    let silence = || {
        lines(&d, "alarm")
            .iter()
            .filter(|l| l["id"].as_str().unwrap().starts_with(prefix))
            .count()
    };
    assert_eq!(silence(), 1);
    fs::write(d.path().join("acks"), "heartbeat:11").unwrap();
    run(&d, &c, 12).unwrap();
    run(&d, &c, 21).unwrap();
    assert_eq!(silence(), 2);
}
