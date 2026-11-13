use pg_worker_core::fleet_series::{annotate, summarize, Sample};
use pg_worker_core::run::record::{RunRecord, Status};
use serde_json::json;

fn sample(t: u64, oom: u64, sessions: &[&str]) -> Sample {
    serde_json::from_value(json!({"host":"worker", "ts":t, "cpu_load":1.0,
        "mem_avail_mb":8000,"swap_used_mb":0,"psi_mem_avg10":0.0,"disk_pct":30.0,
        "cores":4,"runs":2,"ready":1,"sessions":sessions,"sessions_lost":[],
        "oom_kill":oom,"top_rss":[],"reachable":true}))
    .unwrap()
}

#[test]
fn loss_requires_adjacent_reachable_sample_and_oom_increase() {
    let old = sample(60, 3, &["one", "two"]);
    let mut now = sample(120, 4, &["two"]);
    annotate(&mut now, Some(&old));
    assert_eq!(now.sessions_lost, ["one"]);
    for (t, oom) in [(120, 3), (240, 4), (120, 1)] {
        let mut now = sample(t, oom, &[]);
        annotate(&mut now, Some(&old));
        assert!(now.sessions_lost.is_empty());
    }
}

#[test]
fn summary_counts_gaps_resets_and_idle_ready() {
    let mut rows = vec![
        sample(60, 7, &["one"]),
        sample(120, 9, &[]),
        sample(240, 1, &[]),
    ];
    let old = rows[0].clone();
    annotate(&mut rows[1], Some(&old));
    rows[1].runs = 0;
    rows[2].reachable = false;
    let report = summarize(&rows, 60, 300);
    assert_eq!(report["worker"].oom_kill_delta, 2);
    assert_eq!(report["worker"].sessions_lost, 1);
    assert_eq!(report["worker"].unreachable_minutes, 2);
    assert_eq!(report["worker"].runs_p90, 2);
    assert_eq!(report["worker"].runs_mean, 1.0);
    assert!((report["worker"].idle_with_ready_hours - 1.0 / 60.0).abs() < 0.0001);
}

#[test]
fn terminal_record_uses_its_stored_host() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir(tmp.path().join("fleet")).unwrap();
    let rows = [sample(60, 2, &[]), sample(120, 5, &[])];
    let text = rows
        .iter()
        .map(|r| serde_json::to_string(r).unwrap() + "\n")
        .collect::<String>();
    std::fs::write(tmp.path().join("fleet/worker.ndjson"), text).unwrap();
    let mut record = RunRecord::create(
        tmp.path(),
        "run-abc",
        "worker",
        Default::default(),
        Status::Running,
        60.0,
    )
    .unwrap();
    record.transition(Status::Completed, 120.0).unwrap();
    assert_eq!(record.get("oom_kill_delta"), Some(&json!(3)));
    let mut other = RunRecord::create(
        tmp.path(),
        "run-def",
        "other",
        Default::default(),
        Status::Running,
        60.0,
    )
    .unwrap();
    other.transition(Status::Completed, 120.0).unwrap();
    assert_eq!(other.get("oom_kill_delta"), Some(&json!(null)));
}
