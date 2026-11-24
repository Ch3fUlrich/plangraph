use pg_worker_core::worker_host::{
    FloorReport, InstallReport, LegReport, Observation, Phase, RejoinError, RejoinHost, RunFate,
    Store, SyncReport,
};
use std::{collections::BTreeSet, fs, io};
use tempfile::tempdir;

#[derive(Default)]
struct Fake {
    calls: Vec<String>,
    stale_binary: bool,
    fail_sync: bool,
    disk_ok: bool,
    working_legs: Vec<String>,
    fates: Vec<(String, RunFate)>,
    unchecked: Vec<String>,
}

impl Fake {
    fn healthy() -> Self {
        Self {
            disk_ok: true,
            working_legs: vec!["codex".into()],
            fates: vec![
                ("run-finished".into(), RunFate::Finished),
                ("run-live".into(), RunFate::Live),
                ("run-dead".into(), RunFate::Dead),
            ],
            ..Self::default()
        }
    }
}

impl RejoinHost for Fake {
    fn sync_checkouts(&mut self) -> io::Result<SyncReport> {
        self.calls.push("sync".into());
        if self.fail_sync {
            return Err(io::Error::other("fast-forward refused"));
        }
        Ok(SyncReport {
            head: "abc".into(),
            fast_forwarded: vec!["main".into()],
            saved_branches: vec!["saved/main".into()],
        })
    }
    fn check_install(&mut self) -> io::Result<InstallReport> {
        self.calls.push("install".into());
        Ok(InstallReport {
            current: !self.stale_binary,
            detail: "pg-spawnd differs from main".into(),
        })
    }
    fn classify_run(&mut self, run: &str) -> io::Result<RunFate> {
        self.fates
            .iter()
            .find(|(id, _)| id == run)
            .map(|(_, fate)| *fate)
            .ok_or_else(|| io::Error::other("no such run"))
    }
    fn settle_run(&mut self, run: &str, fate: RunFate) -> io::Result<()> {
        self.calls.push(format!("settle {run} {fate:?}"));
        Ok(())
    }
    fn sweep_worktrees(&mut self) -> io::Result<()> {
        self.calls.push("sweep".into());
        Ok(())
    }
    fn check_floors(&mut self) -> io::Result<FloorReport> {
        self.calls.push("floors".into());
        Ok(FloorReport {
            disk_ok: self.disk_ok,
            ram_ok: true,
            detail: "work disks under their floor".into(),
            unchecked: self.unchecked.clone(),
        })
    }
    fn probe_legs(&mut self) -> io::Result<LegReport> {
        self.calls.push("legs".into());
        Ok(LegReport {
            working: self.working_legs.clone(),
            failed: vec!["agy".into()],
        })
    }
}

/// A host that missed probes for more than ten minutes with three runs marked unknown.
fn outage(store: &Store) {
    let tick = |now, ssh| {
        let runs: BTreeSet<String> = ["run-finished", "run-live", "run-dead"]
            .into_iter()
            .map(String::from)
            .collect();
        store
            .observe_liveness(Observation {
                now,
                ssh,
                tailscale: Some(ssh),
                remote_runs: None,
                placed_runs: if ssh { Default::default() } else { runs },
            })
            .unwrap()
    };
    tick(60, false);
    assert_eq!(tick(120, false), Phase::Unreachable);
    assert_eq!(tick(900, true), Phase::Rejoining);
}

#[test]
fn rejoin_runs_every_step_settles_unknown_runs_and_sets_ready() {
    let state = tempdir().unwrap();
    let store = Store::new(state.path(), "worker-a").unwrap();
    outage(&store);
    assert!(store.refusal().unwrap().is_some());
    let mut host = Fake::healthy();
    host.stale_binary = true;
    host.unchecked = vec!["ram: no Windows free-memory source".into()];
    let record = store.rejoin(&mut host, 1000).unwrap();
    assert_eq!(
        host.calls,
        [
            "sync",
            "install",
            "settle run-dead Dead",
            "settle run-finished Finished",
            "settle run-live Live",
            "sweep",
            "floors",
            "legs"
        ]
    );
    assert_eq!(store.phase().unwrap(), Phase::Ready);
    assert_eq!(store.refusal().unwrap(), None);
    assert!(record["flags"][0].as_str().unwrap().starts_with("binary:"));
    assert_eq!(
        record["flags"][1],
        "floor unchecked: ram: no Windows free-memory source"
    );
    assert_eq!(record["runs"].as_array().unwrap().len(), 3);
    let files: Vec<_> = fs::read_dir(state.path().join("rejoin"))
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    assert_eq!(files, ["worker-a-1000-ready.json"]);
    // The next probe clears nothing and the host stays ready.
    assert!(!store.needs_rejoin().unwrap());
}

#[test]
fn a_failed_step_keeps_the_host_out_of_placement_and_records_the_attempt() {
    for (name, fix, step) in [
        (
            "sync",
            (|host: &mut Fake| host.fail_sync = true) as fn(&mut Fake),
            "sync",
        ),
        ("floors", |host: &mut Fake| host.disk_ok = false, "floors"),
        ("legs", |host: &mut Fake| host.working_legs.clear(), "legs"),
    ] {
        let state = tempdir().unwrap();
        let store = Store::new(state.path(), "worker-a").unwrap();
        outage(&store);
        let mut host = Fake::healthy();
        fix(&mut host);
        let error = store.rejoin(&mut host, 1000).unwrap_err();
        assert!(
            matches!(&error, RejoinError::Blocked { step: s, .. } if *s == step),
            "{name}: {error}"
        );
        assert_eq!(store.phase().unwrap(), Phase::Rejoining, "{name}");
        assert!(store.refusal().unwrap().is_some(), "{name}");
        let blocked = state.path().join("rejoin/worker-a-1000-blocked.json");
        assert!(blocked.is_file(), "{name}");
    }
}

#[test]
fn rejoin_refuses_a_host_that_is_ready_or_still_unreachable() {
    let state = tempdir().unwrap();
    let store = Store::new(state.path(), "worker-a").unwrap();
    let mut host = Fake::healthy();
    assert!(matches!(
        store.rejoin(&mut host, 10),
        Err(RejoinError::NotRejoining(_))
    ));
    for now in [60, 120] {
        store
            .observe_liveness(Observation {
                now,
                ssh: false,
                tailscale: None,
                remote_runs: None,
                placed_runs: Default::default(),
            })
            .unwrap();
    }
    assert!(matches!(
        store.rejoin(&mut host, 130),
        Err(RejoinError::NotRejoining("host is unreachable"))
    ));
    assert!(host.calls.is_empty());
}
