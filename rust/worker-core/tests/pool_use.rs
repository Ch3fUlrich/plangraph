use pg_worker_core::pool_use::{record, Pool};
use serde_json::{json, Value};
use std::{fs, process::Command};

/// A window half spent: the pace that uses it by its reset is 50 %.
fn sample(used: f64) -> Value {
    json!({"pool":"chatgpt","window":"weekly","now":50_000,"window_start":0,"reset_at":100_000,
        "used_pct":used,"live":{"cx-gpt-6.2-sol-medium":1},
        "waiting":{"review":3,"implement":2},"admission_slots":4})
}
fn pool(v: Value) -> Pool {
    serde_json::from_value(v).unwrap()
}

#[test]
fn pool_at_20_percent_with_five_ready_cards_starts_runs_up_to_admission() {
    let r = pool(sample(20.0)).report().unwrap();
    assert!(r.underused);
    assert_eq!((r.waiting_total, r.startable), (5, 4));
    assert_eq!(
        r.alert().unwrap(),
        "POOL-UNDERUSE chatgpt window=weekly used=20% pace=50% waiting=5 startable=4"
    );
    // Round robin by leg name, never more than the admission limit.
    assert_eq!(
        r.starts,
        vec![("implement".to_string(), 2), ("review".to_string(), 2)]
    );
    assert_eq!(
        r.start_event().unwrap()["starts"][0],
        json!({"leg":"implement","runs":2})
    );
}

#[test]
fn pool_at_pace_or_without_work_or_after_its_reset_raises_nothing() {
    for used in [35.0, 50.0, 90.0] {
        let r = pool(sample(used)).report().unwrap();
        assert!(
            !r.underused && r.alert().is_none() && r.start_event().is_none(),
            "{used}"
        );
    }
    let mut none = sample(10.0);
    none["waiting"] = json!({});
    assert!(pool(none).report().unwrap().alert().is_none());
    let mut late = sample(10.0);
    late["now"] = json!(100_000);
    let r = pool(late).report().unwrap();
    assert!(r.stale && r.alert().is_none() && r.digest_line().contains("stale"));
    let mut full = sample(10.0);
    full["admission_slots"] = json!(0);
    let r = pool(full).report().unwrap();
    assert!(r.alert().unwrap().ends_with("startable=0") && r.start_event().is_none());
}

#[test]
fn bad_samples_are_errors_never_free_capacity() {
    for (field, bad) in [
        ("used_pct", json!(101.0)),
        ("used_pct", json!(-1.0)),
        ("reset_at", json!(0)),
        ("now", json!(-5)),
        ("pool", json!("a b")),
        ("waiting", json!({"bad leg": 1})),
    ] {
        let mut v = sample(20.0);
        v[field] = bad;
        assert!(pool(v).report().is_err(), "{field}");
    }
}

#[test]
fn digest_line_shows_used_pace_live_and_waiting_per_leg() {
    let r = pool(sample(20.0)).report().unwrap();
    assert_eq!(
        r.digest_line(),
        "POOL chatgpt window=weekly used=20% pace=50% live[cx-gpt-6.2-sol-medium=1] waiting[implement=2,review=3]"
    );
    let mut idle = sample(20.0);
    idle["live"] = json!({});
    idle["waiting"] = json!({});
    assert!(pool(idle)
        .report()
        .unwrap()
        .digest_line()
        .ends_with("live[-] waiting[-]"));
}

#[test]
fn telemetry_rows_are_spaced_ten_minutes_per_window() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("pool-use.jsonl");
    let mut v = sample(20.0);
    assert!(record(&path, &pool(v.clone()).report().unwrap()).unwrap());
    v["now"] = json!(50_300);
    assert!(!record(&path, &pool(v.clone()).report().unwrap()).unwrap());
    v["now"] = json!(50_600);
    assert!(record(&path, &pool(v.clone()).report().unwrap()).unwrap());
    v["window"] = json!("daily");
    assert!(record(&path, &pool(v).report().unwrap()).unwrap());
    let rows: Vec<Value> = fs::read_to_string(&path)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 3);
    assert_eq!(rows[0]["used_pct"], 20.0);
    assert_eq!(
        (rows[0]["startable"].clone(), rows[0]["live"].clone()),
        (json!(4), json!(1))
    );
}

#[test]
fn pool_use_check_prints_digest_alert_and_start_plan_and_records_a_row() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("sample.json");
    let rows = dir.path().join("rows.jsonl");
    fs::write(&input, json!([sample(20.0)]).to_string()).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_pool-use-check"))
        .arg(&input)
        .arg(&rows)
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
    let text = String::from_utf8(out.stdout).unwrap();
    let lines: Vec<_> = text.lines().collect();
    assert!(
        lines[0].starts_with("POOL chatgpt window=weekly used=20%"),
        "{text}"
    );
    assert!(lines[1].starts_with("POOL-UNDERUSE chatgpt"));
    assert_eq!(&lines[2..], ["START implement 2", "START review 2"]);
    assert_eq!(fs::read_to_string(&rows).unwrap().lines().count(), 1);
}

fn watchdog_input(pool: Value) -> Value {
    json!({"pace":0,"budget":1,"ci_failed":0,"ci_total":0,"infra_failed":0,"review_age_hours":0,
        "review_rounds":0,"critical_idle_hours":0,"coding_vm_runs":0,"worker_idle":true,
        "host_drift":false,"closes_recent":0,"closes_previous":0,"pool_use":pool})
}

fn tick(
    dir: &std::path::Path,
    input: &Value,
    start_hook: Option<&std::path::Path>,
) -> std::process::Output {
    fs::write(dir.join("input.json"), input.to_string()).unwrap();
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"));
    cmd.env_remove("PLANGRAPH_WATCHDOG_DELIVERY")
        .env_remove("PLANGRAPH_FLOW_PUSH")
        .env_remove("PLANGRAPH_TURN_BUDGET_STATE_DIR")
        .env_remove("PLANGRAPH_POOL_START");
    if let Some(sink) = start_hook {
        cmd.env("PLANGRAPH_POOL_START", json!(["tee", sink]).to_string());
    }
    let out = cmd.arg(dir.join("input.json")).arg(dir).output().unwrap();
    assert!(out.status.success(), "{out:?}");
    out
}

#[cfg(unix)]
#[test]
fn watchdog_raises_one_underuse_alert_and_hands_the_dispatcher_the_start_plan() {
    let dir = tempfile::tempdir().unwrap();
    let sink = dir.path().join("started.json");
    let input = watchdog_input(sample(20.0));
    let out = tick(dir.path(), &input, Some(&sink));
    let stdout = String::from_utf8(out.stdout).unwrap();
    assert!(
        stdout.contains("POOL chatgpt window=weekly used=20% pace=50%"),
        "{stdout}"
    );
    let queue = fs::read_to_string(dir.path().join("queue.json")).unwrap();
    assert_eq!(queue.matches("POOL-UNDERUSE chatgpt").count(), 1, "{queue}");
    let event: Value = serde_json::from_str(&fs::read_to_string(&sink).unwrap()).unwrap();
    let started: u64 = event["starts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["runs"].as_u64().unwrap())
        .sum();
    assert_eq!(started, 4, "up to the admission limit: {event}");
    // The same standing condition on the next tick queues no second alert.
    tick(dir.path(), &input, Some(&sink));
    let again = fs::read_to_string(dir.path().join("queue.json")).unwrap();
    assert_eq!(again.matches("POOL-UNDERUSE chatgpt").count(), 1, "{again}");
    assert_eq!(
        fs::read_to_string(dir.path().join("pool-use.jsonl"))
            .unwrap()
            .lines()
            .count(),
        1
    );
    // A pool back at pace clears the standing alert; a later breach alerts again.
    tick(dir.path(), &watchdog_input(sample(60.0)), None);
    assert_eq!(fs::read_to_string(dir.path().join("active")).unwrap(), "");
}

#[test]
fn watchdog_keeps_a_standing_underuse_alert_while_the_pool_block_is_missing() {
    let dir = tempfile::tempdir().unwrap();
    tick(dir.path(), &watchdog_input(sample(20.0)), None);
    let mut missing = watchdog_input(Value::Null);
    missing.as_object_mut().unwrap().remove("pool_use");
    tick(dir.path(), &missing, None);
    assert!(fs::read_to_string(dir.path().join("active"))
        .unwrap()
        .contains("pool-underuse chatgpt weekly"));
}
