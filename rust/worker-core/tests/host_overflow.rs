use pg_worker_core::{
    fleet_series::Sample,
    host_overflow::{spawn, Launch},
    host_readiness::{Record, WRITER_SANDBOX},
    package_placement::{Candidate, Policy},
};
use serde_json::json;
fn hosts() -> Vec<Candidate> {
    ["worker", "station", "orchestrator"]
        .into_iter()
        .map(|id| Candidate {
            id: id.into(),
            workstation: id == "station",
            ssh: None,
            readiness: Some(Record {
                host: id.into(),
                sandbox: WRITER_SANDBOX.into(),
                checked_at: 100,
                errors: vec![],
            }),
            sample: Some(Sample {
                host: id.into(),
                ts: 100,
                cores: 12,
                cpu_load: Some(1.0),
                mem_avail_mb: Some(16000),
                reachable: true,
                ..Sample::default()
            }),
        })
        .collect()
}
fn policy() -> Policy {
    Policy {
        allow_workstation: true,
        ..Policy::default()
    }
}
#[test]
fn load_refused_writer_launches_on_workstation_in_same_call() {
    let mut rows = hosts();
    rows[0].sample.as_mut().unwrap().cpu_load = Some(33.0);
    let mut calls = Vec::new();
    let out = spawn(&rows, &policy(), 100, |h| {
        calls.push(h.id.clone());
        Launch::Started(json!({"run_id":"run-abc"}))
    });
    assert_eq!(calls, ["station"]);
    assert_eq!(out.host.as_deref(), Some("station"));
    assert_eq!(out.refusals[0].host, "worker");
    assert_eq!(out.refusals[0].reason, "load gate");
    assert!(!out.queued);
}
#[test]
fn second_gate_refusal_falls_through_to_orchestrator() {
    let mut rows = hosts();
    rows[0].sample.as_mut().unwrap().cpu_load = Some(33.0);
    let mut calls = Vec::new();
    let out = spawn(&rows, &policy(), 100, |h| {
        calls.push(h.id.clone());
        if h.id == "station" {
            Launch::Refused("load rose before admission".into())
        } else {
            Launch::Started(json!({"run_id":"run-abc"}))
        }
    });
    assert_eq!(calls, ["station", "orchestrator"]);
    assert_eq!(out.host.as_deref(), Some("orchestrator"));
    assert_eq!(out.refusals.len(), 2);
    assert!(out.refusals[1].reason.contains("load rose"));
}
#[test]
fn queue_only_when_all_three_refuse() {
    let out = spawn(&hosts(), &policy(), 100, |_| Launch::Refused("busy".into()));
    assert!(out.queued && out.host.is_none());
    assert_eq!(
        out.refusals
            .iter()
            .map(|r| r.host.as_str())
            .collect::<Vec<_>>(),
        ["worker", "station", "orchestrator"]
    );
}
#[test]
fn ambiguous_launch_never_starts_a_duplicate() {
    let mut calls = 0;
    let out = spawn(&hosts(), &policy(), 100, |_| {
        calls += 1;
        Launch::Uncertain("receipt lost".into())
    });
    assert_eq!(calls, 1);
    assert!(!out.queued && out.error.is_some());
}

#[test]
fn overflow_refuses_missing_expired_future_failed_and_wrong_sandbox_readiness() {
    for (change, expected) in [
        ("missing", "missing readiness"),
        ("expired", "stale readiness"),
        ("future", "future readiness"),
        ("failed", "readiness check failed"),
        ("sandbox", "readiness sandbox mismatch"),
        ("host", "readiness host mismatch"),
    ] {
        let mut rows = hosts();
        let record = rows[0].readiness.as_mut().unwrap();
        record.checked_at = 300;
        match change {
            "missing" => rows[0].readiness = None,
            "expired" => record.checked_at = 1,
            "future" => record.checked_at = 301,
            "failed" => record.errors.push("sandbox cannot start".into()),
            "sandbox" => record.sandbox = "danger-full-access".into(),
            "host" => record.host = "station".into(),
            _ => unreachable!(),
        }
        for row in &mut rows {
            row.sample.as_mut().unwrap().ts = 300;
        }
        rows[1].readiness.as_mut().unwrap().checked_at = 300;
        rows[2].readiness.as_mut().unwrap().checked_at = 300;
        let out = spawn(&rows, &policy(), 300, |h| Launch::Started(json!(h.id)));
        assert_eq!(out.host.as_deref(), Some("station"));
        assert!(
            out.refusals[0].reason.contains(expected),
            "{}",
            out.refusals[0].reason
        );
    }
}

#[test]
fn known_link_outage_queues_without_changing_hosts() {
    let mut attempted = Vec::new();
    let out = spawn(&hosts(), &policy(), 100, |host| {
        attempted.push(host.id.clone());
        Launch::Waiting(format!("{} unreachable since 90", host.id))
    });
    assert_eq!(attempted, ["worker"]);
    assert!(out.queued && out.error.is_none());
    assert_eq!(out.refusals[0].reason, "worker unreachable since 90");
}
