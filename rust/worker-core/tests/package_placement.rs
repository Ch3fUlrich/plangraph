use pg_worker_core::fleet_series::Sample;
use pg_worker_core::package_placement::{choose, Candidate, Policy};

fn host(id: &str, mem: u64, load: f64) -> Candidate {
    Candidate {
        id: id.into(),
        workstation: false,
        ssh: None,
        sample: Some(Sample {
            host: id.into(),
            ts: 100,
            cores: 4,
            reachable: true,
            cpu_load: Some(load),
            mem_avail_mb: Some(mem),
            swap_used_mb: Some(0),
            ..Sample::default()
        }),
    }
}
#[test]
fn selects_admitting_host_and_explains_floor() {
    let result = choose(
        &[
            host("worker", 2000, 0.1),
            host("station", 16000, 1.0),
            host("orchestrator", 8000, 2.0),
        ],
        &Policy::default(),
        100,
    );
    assert_eq!(result.host.as_deref(), Some("station"));
    assert!(result.refusals[0].reason.contains("memory"));
    assert!(result.reason.contains("load/core"));
}
#[test]
fn all_busy_queues_and_unreachable_is_refused() {
    let mut hosts = vec![
        host("worker", 8000, 5.0),
        host("station", 8000, 5.0),
        host("orchestrator", 8000, 1.0),
    ];
    hosts[2].sample.as_mut().unwrap().reachable = false;
    let result = choose(&hosts, &Policy::default(), 100);
    assert!(result.host.is_none());
    assert_eq!(result.refusals.len(), 3);
    assert_eq!(result.refusals[2].reason, "unreachable");
}
#[test]
fn stale_future_unknown_and_workstation_permission_fail_closed() {
    let mut row = host("station", 16000, 0.0);
    row.workstation = true;
    assert!(choose(&[row.clone()], &Policy::default(), 100)
        .host
        .is_none());
    let policy = Policy {
        allow_workstation: true,
        ..Policy::default()
    };
    assert!(choose(&[row.clone()], &policy, 100).host.is_some());
    for stamp in [0, 101] {
        row.sample.as_mut().unwrap().ts = stamp;
        assert!(choose(&[row.clone()], &policy, 300).host.is_none());
    }
    row.sample.as_mut().unwrap().ts = 300;
    row.sample.as_mut().unwrap().cpu_load = None;
    assert!(choose(&[row], &policy, 300).host.is_none());
}

#[test]
fn refresh_probes_only_stale_samples_and_records_unreachable() {
    let mut rows = vec![host("fresh", 9000, 0.0), host("stale", 9000, 0.0)];
    rows[1].sample.as_mut().unwrap().ts = 0;
    let mut called = Vec::new();
    pg_worker_core::package_placement::refresh_with(&mut rows, 200, |h| {
        called.push(h.id.clone());
        Err(std::io::Error::other("offline"))
    });
    assert_eq!(called, ["stale"]);
    assert_eq!(
        choose(&rows, &Policy::default(), 200).host.as_deref(),
        Some("fresh")
    );
    assert!(!rows[1].sample.as_ref().unwrap().reachable);
}
