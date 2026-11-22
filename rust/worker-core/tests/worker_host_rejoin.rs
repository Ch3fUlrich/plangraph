use pg_worker_core::{
    package_placement::{choose, Candidate, Policy},
    worker_host::{Phase, Step, Store},
};
use tempfile::tempdir;

#[test]
fn a_long_outage_requires_all_onboarding_steps_before_placement() {
    let state = tempdir().unwrap();
    let host = Store::new(state.path(), "worker-a").unwrap();
    host.observe(60, false, None).unwrap();
    host.observe(120, false, None).unwrap();
    assert_eq!(host.phase().unwrap(), Phase::Unreachable);
    host.observe(661, true, None).unwrap();
    assert_eq!(host.phase().unwrap(), Phase::Rejoining);

    let candidate = Candidate {
        id: "worker-a".into(),
        workstation: false,
        ssh: None,
        sample: None,
        readiness: None,
    };
    let policy = Policy {
        worker_host_state: Some(state.path().into()),
        ..Policy::default()
    };
    let choice = choose(&[candidate], &policy, 661);
    assert!(choice.host.is_none());
    assert_eq!(choice.refusals[0].reason, "worker host rejoining");

    let mut steps = Vec::new();
    let result = host.rejoin(662, |step| {
        steps.push(step);
        if step == Step::Install {
            Err("installed binary is stale".into())
        } else {
            Ok(serde_json::Value::Null)
        }
    });
    assert!(result.is_err());
    assert!(steps.contains(&Step::Sync));
    assert!(steps.contains(&Step::Runs));
    assert_eq!(host.phase().unwrap(), Phase::Rejoining);

    let mut steps = Vec::new();
    host.rejoin(663, |step| {
        steps.push(step);
        Ok(serde_json::Value::Null)
    })
    .unwrap();
    assert_eq!(
        steps,
        [
            Step::Sync,
            Step::Install,
            Step::Runs,
            Step::Worktrees,
            Step::Capacity,
            Step::Legs
        ]
    );
    assert_eq!(host.phase().unwrap(), Phase::Ready);
}

#[test]
fn a_second_outage_invalidates_an_in_flight_rejoin() {
    let state = tempdir().unwrap();
    let host = Store::new(state.path(), "worker-a").unwrap();
    host.observe(60, false, None).unwrap();
    host.observe(661, true, None).unwrap();
    assert!(host
        .rejoin(662, |step| {
            if step == Step::Legs {
                host.observe(663, false, None).unwrap();
                host.observe(664, true, None).unwrap();
            }
            Ok(serde_json::Value::Null)
        })
        .is_err());
    assert_eq!(host.phase().unwrap(), Phase::Rejoining);
}

#[test]
fn maintenance_suppresses_paging_and_recovery_requires_rejoin() {
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
    assert_eq!(tick(20, false).unwrap(), (Phase::Rejoining, false));
    assert_eq!(tick(80, false).unwrap(), (Phase::Rejoining, false));
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
    assert_eq!(tick(150, false).unwrap(), (Phase::Rejoining, false));
    assert_eq!(read()[1]["rule"], "worker-host-unreachable");
    assert_eq!(read()[1]["recipient"], "routing");
    assert_eq!(read()[1]["maintenance_until"], 150);
    assert_eq!(tick(210, true).unwrap(), (Phase::Rejoining, true));
    assert_eq!(read()[2]["rule"], "worker-host-recovered");
    assert_eq!(
        read()[2]["unknown_run_ids"],
        serde_json::json!(["run-example"])
    );
    assert!(store.unknown_runs().unwrap().contains("run-example"));
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
