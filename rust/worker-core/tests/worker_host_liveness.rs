use pg_worker_core::worker_host::{Phase, Store};
use tempfile::tempdir;

#[test]
fn short_outage_recovers_without_an_onboarding_command() {
    use pg_worker_core::worker_host::Observation;

    let state = tempdir().unwrap();
    let store = Store::new(state.path(), "worker-a").unwrap();
    let tick = |now, ssh| {
        store
            .observe_liveness(Observation {
                now,
                ssh,
                tailscale: Some(ssh),
                remote_runs: None,
                placed_runs: Default::default(),
            })
            .unwrap()
    };
    assert_eq!(tick(60, false), Phase::Ready);
    assert_eq!(tick(120, false), Phase::Unreachable);
    assert_eq!(tick(180, true), Phase::Ready);
}

#[test]
fn outage_over_ten_minutes_holds_the_host_in_rejoining_until_the_gate_passes() {
    use pg_worker_core::worker_host::Observation;

    let state = tempdir().unwrap();
    let store = Store::new(state.path(), "worker-a").unwrap();
    let tick = |now, ssh| {
        store
            .observe_liveness(Observation {
                now,
                ssh,
                tailscale: Some(ssh),
                remote_runs: None,
                placed_runs: Default::default(),
            })
            .unwrap()
    };
    assert_eq!(tick(60, false), Phase::Ready);
    assert_eq!(tick(120, false), Phase::Unreachable);
    assert_eq!(tick(720, false), Phase::Unreachable);
    assert_eq!(tick(780, true), Phase::Rejoining);
    assert!(store.refusal().unwrap().unwrap().contains("rejoining"));
    // Further reachable probes do not lift the hold.
    assert_eq!(tick(840, true), Phase::Rejoining);
}

#[test]
fn maintenance_suppresses_paging_until_expiry() {
    use pg_worker_core::worker_host::Observation;
    use serde_json::Value;
    use std::{collections::BTreeSet, fs};

    let state = tempdir().unwrap();
    let store = Store::new(state.path(), "worker-a").unwrap();
    store.set_maintenance(10, 150).unwrap();
    assert_eq!(store.phase().unwrap(), Phase::Rejoining);
    let tick = |now, ssh| {
        store.observe_liveness(Observation {
            now,
            ssh,
            tailscale: Some(ssh),
            remote_runs: None,
            placed_runs: BTreeSet::from(["run-example".into()]),
        })
    };
    assert_eq!(tick(20, false).unwrap(), Phase::Rejoining);
    assert_eq!(tick(80, false).unwrap(), Phase::Rejoining);
    let dir = state.path().join("watchdog/outbox");
    let read = || -> Vec<Value> {
        let mut paths = fs::read_dir(&dir)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect::<Vec<_>>();
        paths.sort();
        paths
            .iter()
            .map(|path| serde_json::from_slice(&fs::read(path).unwrap()).unwrap())
            .collect()
    };
    assert_eq!(read()[0]["recipient"], "host-health");
    assert_eq!(read()[0]["maintenance_until"], 150);
    assert_eq!(tick(150, false).unwrap(), Phase::Unreachable);
    assert_eq!(read()[1]["rule"], "worker-host-unreachable");
    assert_eq!(read()[1]["recipient"], "routing");
    assert_eq!(read()[1]["maintenance_until"], 150);
    assert_eq!(tick(210, true).unwrap(), Phase::Ready);
    assert_eq!(read()[2]["rule"], "worker-host-recovered");
    assert_eq!(
        read()[2]["unknown_run_ids"],
        serde_json::json!(["run-example"])
    );
}

#[test]
fn an_outbox_failure_leaves_placement_blocked_and_retries_the_alert() {
    use pg_worker_core::worker_host::Observation;
    use std::{collections::BTreeSet, fs};

    let state = tempdir().unwrap();
    let store = Store::new(state.path(), "worker-a").unwrap();
    let dir = state.path().join("watchdog/outbox");
    fs::create_dir_all(dir.parent().unwrap()).unwrap();
    fs::write(&dir, "blocked").unwrap();
    let tick = |now| {
        store.observe_liveness(Observation {
            now,
            ssh: false,
            tailscale: Some(false),
            remote_runs: None,
            placed_runs: BTreeSet::new(),
        })
    };
    tick(10).unwrap();
    assert!(tick(70).is_err());
    assert_eq!(store.phase().unwrap(), Phase::Unreachable);
    fs::remove_file(&dir).unwrap();
    tick(130).unwrap();
    tick(190).unwrap();
    assert_eq!(fs::read_dir(dir).unwrap().count(), 1);
}
