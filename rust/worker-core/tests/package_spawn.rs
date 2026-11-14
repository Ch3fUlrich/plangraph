use pg_worker_core::fleet_series::{self, Sample};
use serde_json::{json, Value};
use std::{
    fs,
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};
#[test]
fn busy_fleet_queues_with_persisted_refusals_without_any_launch() {
    let tmp = tempfile::tempdir().unwrap();
    let mut registry: Value = serde_json::from_str(include_str!(
        "../../../docs/schemas/examples/hosts.example.json"
    ))
    .unwrap();
    let base = registry["hosts"][0].clone();
    let ids = ["worker", "station", "orchestrator"];
    registry["self"] = "worker".into();
    registry["hosts"] = ids
        .iter()
        .map(|id| {
            let mut row = base.clone();
            row["id"] = (*id).into();
            row["aliases"] = json!([]);
            row
        })
        .collect();
    let registry_path = tmp.path().join("hosts.json");
    fs::write(&registry_path, registry.to_string()).unwrap();
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    for id in ids {
        fleet_series::append(
            &tmp.path().join("fleet"),
            &mut Sample {
                host: id.into(),
                ts: now,
                mem_avail_mb: Some(16000),
                cpu_load: Some(33.0),
                cores: 12,
                reachable: true,
                ..Sample::default()
            },
        )
        .unwrap();
    }
    let request = tmp.path().join("request.json");
    fs::write(
        &request,
        json!({"card":{"x":1},"prompt":"fixture","role":"implement"}).to_string(),
    )
    .unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_pg-package-host"))
        .current_dir(tmp.path())
        .env("HOME", tmp.path())
        .arg("--registry")
        .arg(registry_path)
        .arg("--state")
        .arg(tmp.path())
        .arg("--spawn")
        .arg(request)
        .output()
        .unwrap();
    assert_eq!(
        out.status.code(),
        Some(5),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let value: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(value["queued"], true);
    assert_eq!(value["refusals"].as_array().unwrap().len(), 3);
    assert_eq!(
        fs::read_dir(tmp.path().join("overflow")).unwrap().count(),
        1
    );
}
