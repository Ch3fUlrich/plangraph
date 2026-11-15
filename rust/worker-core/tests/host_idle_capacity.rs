use std::{fs, process::Command};

fn snapshot() -> serde_json::Value {
    serde_json::json!({
        "hosts": [{"host":"fixture-host", "caps": {
            "max_runs":8,"max_writers":4,"max_seats":4,"max_sessions":2,"mem_floor_mb":3000},
            "table":{"version":1,"active":[],"queued":[]},"rss_mb":{},
            "mem_available_mb":26000,"writer_mb":2048,"disk_free_bytes":20000000000u64,
            "cpus":32,"load1":3.0}],
        "ready_items":[{"key":"demo:A","ready":true,"blocked":false,"claimed":false}]
    })
}

fn check(value: &serde_json::Value) -> std::process::Output {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("input.json");
    fs::write(&input, value.to_string()).unwrap();
    // Resolve the binary at runtime so this regression runs (and fails) on main.
    let binary = std::path::Path::new(env!("CARGO_BIN_EXE_flow-watchdog")).with_file_name(format!(
        "host-idle-capacity{}",
        std::env::consts::EXE_SUFFIX
    ));
    let output = Command::new(binary)
        .arg(input)
        .output()
        .expect("host-idle-capacity executable");
    assert!(output.status.success(), "{output:?}");
    output
}

#[test]
fn idle_capacity_prints_only_admissible_hosts_with_unclaimed_ready_work() {
    let value = snapshot();
    let output = check(&value);
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "IDLE fixture-host slots=4 ready=1\n"
    );
    for (field, full) in [
        ("mem_available_mb", serde_json::json!(3000)),
        ("disk_free_bytes", serde_json::json!(0)),
        ("load1", serde_json::json!(32.0)),
    ] {
        let mut value = snapshot();
        value["hosts"][0][field] = full;
        assert!(check(&value).stdout.is_empty(), "{field}");
    }
    for field in ["blocked", "claimed", "ready"] {
        let mut value = snapshot();
        value["ready_items"][0][field] = (field != "ready").into();
        assert!(check(&value).stdout.is_empty());
    }
    let mut value = snapshot();
    value["ready_items"] = serde_json::json!([]);
    assert!(check(&value).stdout.is_empty());
}

#[test]
fn idle_capacity_watchdog_queues_orchestrator_alarm() {
    let dir = tempfile::tempdir().unwrap();
    let mut value = serde_json::json!({"pace":0,"budget":1,"ci_failed":0,"ci_total":0,
        "infra_failed":0,"review_age_hours":0,"review_rounds":0,"critical_idle_hours":0,
        "coding_vm_runs":0,"worker_idle":true,"host_drift":false,
        "closes_recent":0,"closes_previous":0});
    value["host_idle_capacity"] = snapshot();
    let input = dir.path().join("input.json");
    fs::write(&input, value.to_string()).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .env_remove("PLANGRAPH_WATCHDOG_DELIVERY")
        .env_remove("PLANGRAPH_FLOW_PUSH")
        .env_remove("PLANGRAPH_TURN_BUDGET_STATE_DIR")
        .arg(input)
        .arg(dir.path())
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let queue = fs::read_to_string(dir.path().join("queue.json")).unwrap_or_default();
    assert!(
        queue.contains("IDLE fixture-host slots=4 ready=1"),
        "{queue}"
    );
    assert!(queue.contains("routing"));
    let mut second = value.clone();
    second["host_idle_capacity"]["ready_items"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({"key":"demo:B","ready":true,"blocked":false,"claimed":false}));
    fs::write(dir.path().join("input.json"), second.to_string()).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .env_remove("PLANGRAPH_WATCHDOG_DELIVERY")
        .env_remove("PLANGRAPH_FLOW_PUSH")
        .env_remove("PLANGRAPH_TURN_BUDGET_STATE_DIR")
        .arg(dir.path().join("input.json"))
        .arg(dir.path())
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "IDLE fixture-host slots=4 ready=2\n"
    );
    let queue: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(dir.path().join("queue.json")).unwrap()).unwrap();
    assert_eq!(queue.as_array().unwrap().len(), 1);
}

#[test]
fn idle_writer_capacity_reuses_admission_caps_and_unmaterialized_ram() {
    use pg_worker_core::{
        hosts::Caps,
        slots::{writer_capacity, ActiveSlot, SlotClass},
    };
    let limits: Caps = serde_json::from_value(snapshot()["hosts"][0]["caps"].clone()).unwrap();
    let writer: ActiveSlot = serde_json::from_value(serde_json::json!({
        "run_id":"run-a","pid":1,"start_time":1,"role":"implement","class":"writer",
        "repo_id":"demo","reservation_mb":2048,"created_ms":1,"bound":true
    }))
    .unwrap();
    let active = std::slice::from_ref(&writer);
    assert_eq!(writer_capacity(&[], &limits, 26000, |_| 0, 2048), 4);
    assert_eq!(writer_capacity(active, &limits, 26000, |_| 0, 2048), 3);
    // Only the unmaterialized promise is subtracted from MemAvailable.
    assert_eq!(writer_capacity(active, &limits, 7096, |_| 0, 2048), 1);
    assert_eq!(writer_capacity(active, &limits, 7096, |_| 2048, 2048), 2);
    assert_eq!(writer_capacity(&[], &limits, 5047, |_| 0, 2048), 0);
    assert_eq!(writer_capacity(&[], &limits, 5048, |_| 0, 2048), 1);
    assert_eq!(writer_capacity(&[], &limits, 26000, |_| 0, 0), 0);
    let mut full = vec![writer; 4];
    assert_eq!(writer_capacity(&full, &limits, 26000, |_| 0, 2048), 0);
    for slot in &mut full {
        slot.class = SlotClass::Seat;
    }
    full.extend(full.clone());
    assert_eq!(writer_capacity(&full, &limits, 26000, |_| 0, 2048), 0);
}

#[test]
fn idle_capacity_deduplicates_ready_and_rejects_conflicting_rows() {
    let mut value = snapshot();
    let item = value["ready_items"][0].clone();
    value["ready_items"].as_array_mut().unwrap().push(item);
    assert_eq!(
        String::from_utf8(check(&value).stdout).unwrap(),
        "IDLE fixture-host slots=4 ready=1\n"
    );
    value["ready_items"][1]["claimed"] = true.into();
    assert!(check(&value).stdout.is_empty());
}
