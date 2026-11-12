use super::*;
use crate::hosts::{HostEntry, Os, Transport, Warmup};
use serde_json::json;

fn registry() -> HostRegistry {
    HostRegistry {
        version: 1,
        self_host: "alpha".into(),
        hosts: ["beta", "alpha"]
            .into_iter()
            .map(|id| HostEntry {
                id: id.into(),
                class: HostClass::Worker,
                aliases: vec![format!("{id}-alias")],
                transport: Transport::Local,
                ssh_target: None,
                env: BTreeMap::new(),
                os: Os::Linux,
                state_dir: String::new(),
                caps: Caps {
                    max_runs: 2,
                    max_writers: 1,
                    max_seats: 1,
                    max_sessions: 1,
                    mem_floor_mb: 1,
                },
                warmup: Warmup {
                    warmup_s: 0,
                    ramp_mb: BTreeMap::new(),
                },
                session_mb: 1,
                roles: vec![],
                repos: BTreeMap::new(),
            })
            .collect(),
    }
}

fn sample() -> HostSample {
    HostSample {
        host_id: "alpha".into(),
        received_at_ms: 1,
        cpu_percent: None,
        ram_available_bytes: None,
        gpu_percent: None,
        network_rx_bytes_per_s: None,
        network_tx_bytes_per_s: None,
        disk_free_bytes: None,
    }
}

fn record(host: &str, id: &str, status: &str) -> Value {
    json!({"host": host, "run_id": id, "status": status})
}

fn counts(records: Option<&[Value]>) -> Vec<Option<u64>> {
    let summary = summarize(&registry(), &[], records);
    vec![
        summary.running_agents,
        summary.hosts[0].running_agents,
        summary.hosts[1].running_agents,
    ]
}

#[test]
fn unreadable_each_metric_is_null() {
    let value = serde_json::to_value(normalize_sample(sample())).unwrap();
    for key in [
        "cpu_percent",
        "ram_available_bytes",
        "gpu_percent",
        "network_rx_bytes_per_s",
        "network_tx_bytes_per_s",
        "disk_free_bytes",
    ] {
        assert!(value.as_object().unwrap().contains_key(key));
        assert!(value[key].is_null());
    }
    let decoded: HostSample = serde_json::from_value(value).unwrap();
    assert!(decoded.cpu_percent.is_none());
}

#[test]
fn measured_zero_survives() {
    let mut s = sample();
    s.cpu_percent = Some(0.0);
    s.gpu_percent = Some(0.0);
    s.ram_available_bytes = Some(0);
    s.network_rx_bytes_per_s = Some(0);
    s.network_tx_bytes_per_s = Some(0);
    s.disk_free_bytes = Some(0);
    let value = serde_json::to_value(normalize_sample(s)).unwrap();
    for key in ["cpu_percent", "gpu_percent"] {
        assert_eq!(value[key], json!(0.0));
    }
    for key in [
        "ram_available_bytes",
        "network_rx_bytes_per_s",
        "network_tx_bytes_per_s",
        "disk_free_bytes",
    ] {
        assert_eq!(value[key], json!(0));
    }
}

#[test]
fn invalid_percent_is_unknown() {
    for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, -0.1, 100.1] {
        let mut s = sample();
        s.cpu_percent = Some(invalid);
        s.gpu_percent = Some(100.0);
        let s = normalize_sample(s);
        assert_eq!(s.cpu_percent, None);
        assert_eq!(s.gpu_percent, Some(100.0));
        let mut s = sample();
        s.cpu_percent = Some(50.0);
        s.gpu_percent = Some(invalid);
        let s = normalize_sample(s);
        assert_eq!(s.cpu_percent, Some(50.0));
        assert_eq!(s.gpu_percent, None);
    }
}

#[test]
fn newest_sample_and_tie() {
    let mut newest = sample();
    newest.received_at_ms = 3;
    newest.cpu_percent = Some(10.0);
    let mut tie = newest.clone();
    tie.cpu_percent = Some(20.0);
    let summary = summarize(&registry(), &[newest, sample(), tie], Some(&[]));
    assert_eq!(
        summary.hosts[0].sample.as_ref().unwrap().cpu_percent,
        Some(20.0)
    );
}

#[test]
fn every_registry_host_has_row() {
    let mut reg = registry();
    reg.hosts[0].class = HostClass::Restricted;
    let result = summarize(&reg, &[sample()], Some(&[]));
    assert_eq!(
        result
            .hosts
            .iter()
            .map(|r| r.host_id.as_str())
            .collect::<Vec<_>>(),
        ["alpha", "beta"]
    );
    assert!(result.hosts[1].sample.is_none());
    assert_eq!(result.hosts[1].class, HostClass::Restricted);
    assert_eq!(result.hosts[1].caps.max_runs, 2);
    let value = serde_json::to_value(result).unwrap();
    assert!(value["hosts"][1]["sample"].is_null());
}

#[test]
fn running_records_not_process_samples() {
    let mut s = sample();
    s.cpu_percent = Some(100.0);
    let records: Vec<_> = [
        "running",
        "claimed",
        "unknown",
        "cancelling",
        "completed",
        "queued",
        "needs_input",
    ]
    .into_iter()
    .enumerate()
    .map(|(i, status)| record("alpha", &format!("run-{i}"), status))
    .collect();
    let result = summarize(&registry(), &[s], Some(&records));
    assert_eq!(result.running_agents, Some(1));
    assert_eq!(counts(Some(&[])), [Some(0), Some(0), Some(0)]);
}

#[test]
fn duplicates_do_not_double_count() {
    let mut a = record("alpha", "run-a", "running");
    a["attempts"] = json!([{}, {}]);
    assert_eq!(
        counts(Some(&[a.clone(), a, record("beta", "run-a", "running")])),
        [Some(2), Some(1), Some(1)]
    );
}

#[test]
fn conflicting_records_are_unknown() {
    for statuses in [
        ["running", "completed"],
        ["completed", "running"],
        ["queued", "failed"],
    ] {
        assert_eq!(
            counts(Some(&[
                record("alpha", "run-a", statuses[0]),
                record("alpha", "run-a", statuses[1])
            ])),
            [None, None, Some(0)]
        );
    }
    for bad in [
        json!({"host":"alpha"}),
        record("alpha", "invalid", "running"),
        record("alpha", "run-a", "RUNNING"),
        json!({"host":"alpha","run_id":"run-a","status":4}),
    ] {
        assert_eq!(counts(Some(&[bad])), [None, None, Some(0)]);
    }
    for bad in [Value::Null, json!([]), json!({}), json!({"host": 1})] {
        assert_eq!(counts(Some(&[bad])), [None, None, None]);
    }
}

#[test]
fn missing_records_unknown_empty_records_zero() {
    assert_eq!(counts(None), [None, None, None]);
    assert_eq!(counts(Some(&[])), [Some(0), Some(0), Some(0)]);
}

#[test]
fn unknown_host_not_silently_lost() {
    assert_eq!(
        counts(Some(&[record("unregistered", "run-a", "running")])),
        [None, None, None]
    );
    let mut s = sample();
    s.host_id = "unregistered".into();
    assert!(summarize(&registry(), &[s], Some(&[]))
        .hosts
        .iter()
        .all(|r| r.sample.is_none()));
}

#[test]
fn aliases_are_canonical() {
    let mut s = sample();
    s.host_id = "ALPHA-ALIAS".into();
    s.gpu_percent = Some(-1.0);
    let records = [
        record("ALPHA-ALIAS", "run-a", "running"),
        record("alpha", "run-a", "running"),
    ];
    let result = summarize(&registry(), &[s], Some(&records));
    let sample = result.hosts[0].sample.as_ref().unwrap();
    assert_eq!(sample.host_id, "alpha");
    assert_eq!(sample.gpu_percent, None);
    assert_eq!(result.running_agents, Some(1));
}
