//! The daemon binds a durable slot before each launcher call and frees a proven pre-exec bind.
use super::{
    finalize,
    group::*,
    record::{RunRecord, Status},
    slot_bind::*,
    supervise::*,
    supervisor::*,
};
use crate::{
    hosts::Role,
    slots::{self, ActiveSlot, GroupLiveness, GroupState, SlotClass, SlotTable, Ticket},
};
use serde_json::json;
use std::{
    path::Path,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

const DAEMON: (u32, u64) = (4_000_001, 77);
const HOLD: &str = "i=0; while [ ! -e go ] && [ $i -lt 400 ]; do sleep .01; i=$((i+1)); done";

fn table(dir: &Path) -> SlotTable {
    slots::with_table(dir, Duration::from_secs(2), |t| t.clone()).unwrap()
}
fn bind_cfg(dir: &Path) -> SlotBindCfg {
    SlotBindCfg {
        slots_dir: dir.into(),
        daemon_pid: DAEMON.0,
        daemon_start_time: DAEMON.1,
        role: Role::Implement,
        class: SlotClass::Writer,
        repo_id: "repo".into(),
        reservation_mb: 512,
    }
}
/// A fake launcher: counts calls and notes whether the daemon's bound row was in the table at
/// the moment of each call, then starts the real (plain) process.
struct Counting {
    calls: AtomicUsize,
    bound_at_call: Mutex<Vec<bool>>,
    slots_dir: std::path::PathBuf,
}
impl Launcher for Counting {
    fn launch(
        &self,
        unit: &str,
        spec: &LaunchSpec<'_>,
        c: &Confinement,
        l: &RunLimits,
    ) -> Result<Grouped, LaunchError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        let bound = table(&self.slots_dir).active.iter().any(|s| {
            s.bound && s.group.as_deref() == Some(unit) && (s.pid, s.start_time) == DAEMON
        });
        self.bound_at_call.lock().unwrap().push(bound);
        SystemLauncher.launch(unit, spec, c, l)
    }
}
fn supervisor(dir: &Path, max_runs: usize) -> (Supervisor, Arc<Counting>) {
    let mut cfg = SupervisorCfg::new(Config {
        state_dir: dir.into(),
        confinement: Confinement::Plain,
        grace: Duration::from_millis(30),
        kill_timeout: Duration::from_millis(50),
        reader_grace: Duration::from_millis(50),
        held_poll: Duration::from_millis(10),
        ..Default::default()
    });
    cfg.max_runs = max_runs;
    cfg.slot_bind = Some(bind_cfg(dir));
    let launcher = Arc::new(Counting {
        calls: AtomicUsize::new(0),
        bound_at_call: Mutex::new(Vec::new()),
        slots_dir: dir.into(),
    });
    (
        Supervisor::with_launcher(cfg, launcher.clone()).unwrap(),
        launcher,
    )
}
fn spec(dir: &Path, n: usize, script: &str) -> RunSpec {
    RunSpec {
        run_id: format!("run-{n}"),
        job_id: "job-test".into(),
        program: "sh".into(),
        args: vec!["-c".into(), script.into()],
        cwd: dir.into(),
        env: vec![],
        stdin: None,
        base: Default::default(),
        events: vec![],
        finish: None,
        allow_shas: Default::default(),
    }
}
fn until(mut f: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(8);
    while !f() {
        assert!(Instant::now() < deadline, "deadline exceeded");
        thread::sleep(Duration::from_millis(10));
    }
}
fn status(dir: &Path, n: usize) -> String {
    RunRecord::read(dir, &format!("run-{n}"))
        .unwrap()
        .to_value()["status"]
        .as_str()
        .unwrap()
        .into()
}

#[test]
fn a_pending_request_is_durable_and_makes_no_launcher_call() {
    let d = tempfile::tempdir().unwrap();
    let (s, launcher) = supervisor(d.path(), 1);
    s.start(spec(d.path(), 0, HOLD)).unwrap();
    until(|| status(d.path(), 0) == "running");
    s.start(spec(d.path(), 1, "true")).unwrap();
    until(|| table(d.path()).queued.iter().any(|t| t.run_id == "run-1"));
    let t = table(d.path());
    let ticket = t.queued.iter().find(|t| t.run_id == "run-1").unwrap();
    assert_eq!((ticket.pid, ticket.start_time), DAEMON);
    assert!(t.active.iter().all(|a| a.run_id != "run-1"));
    // The queued run was not launched: only run-0 reached the launcher.
    assert_eq!(launcher.calls.load(Ordering::SeqCst), 1);
    assert_eq!(status(d.path(), 1), "queued");
    std::fs::write(d.path().join("go"), "").unwrap();
    until(|| status(d.path(), 1) == "completed" || status(d.path(), 1) == "failed");
    assert_eq!(launcher.calls.load(Ordering::SeqCst), 2);
}

#[test]
fn a_refused_bind_makes_no_launcher_call() {
    let d = tempfile::tempdir().unwrap();
    // Another owner already holds a slot of this run id.
    slots::with_table(d.path(), Duration::from_secs(2), |t| {
        t.active.push(ActiveSlot {
            run_id: "run-3".into(),
            pid: 99,
            start_time: 5,
            role: Role::Implement,
            class: SlotClass::Writer,
            repo_id: "other".into(),
            seat_slot: None,
            group: None,
            reservation_mb: 1,
            created_ms: 1,
            bound: false,
        })
    })
    .unwrap();
    let (s, launcher) = supervisor(d.path(), 2);
    s.start(spec(d.path(), 3, "true")).unwrap();
    until(|| status(d.path(), 3) == "failed");
    assert_eq!(launcher.calls.load(Ordering::SeqCst), 0);
    let v = RunRecord::read(d.path(), "run-3").unwrap().to_value();
    assert_eq!(v["reason"], "admission refused");
    // The other owner's row is untouched and no ticket of ours is left behind.
    let t = table(d.path());
    assert_eq!(t.active.len(), 1);
    assert_eq!(t.active[0].pid, 99);
    assert!(t.queued.is_empty());
    // The refused run freed its local place: the next run is admitted.
    s.start(spec(d.path(), 4, "true")).unwrap();
    until(|| status(d.path(), 4) == "completed");
    assert_eq!(launcher.calls.load(Ordering::SeqCst), 1);
}

#[test]
fn a_bound_run_launches_once_with_its_reservation_and_frees_it_at_the_end() {
    let d = tempfile::tempdir().unwrap();
    let (s, launcher) = supervisor(d.path(), 2);
    s.start(spec(d.path(), 0, HOLD)).unwrap();
    until(|| status(d.path(), 0) == "running");
    let pid = RunRecord::read(d.path(), "run-0").unwrap().to_value()["pid"]
        .as_u64()
        .unwrap();
    // Once launched, the row is keyed by the run's own process, not the daemon.
    until(|| {
        table(d.path())
            .active
            .iter()
            .any(|a| u64::from(a.pid) == pid)
    });
    let t = table(d.path());
    let row = t.active.iter().find(|a| a.run_id == "run-0").unwrap();
    assert!(row.bound);
    assert_eq!(row.reservation_mb, 512);
    assert_ne!(row.pid, DAEMON.0);
    assert!(t.queued.is_empty());
    std::fs::write(d.path().join("go"), "").unwrap();
    until(|| status(d.path(), 0) == "completed");
    until(|| table(d.path()).active.is_empty());
    assert_eq!(launcher.calls.load(Ordering::SeqCst), 1);
    assert_eq!(*launcher.bound_at_call.lock().unwrap(), [true]);
}

#[test]
fn a_cancelled_pending_run_leaves_no_ticket() {
    let d = tempfile::tempdir().unwrap();
    let (s, launcher) = supervisor(d.path(), 1);
    s.start(spec(d.path(), 0, HOLD)).unwrap();
    until(|| status(d.path(), 0) == "running");
    s.start(spec(d.path(), 1, "true")).unwrap();
    until(|| !table(d.path()).queued.is_empty());
    s.cancel("run-1");
    until(|| table(d.path()).queued.is_empty());
    std::fs::write(d.path().join("go"), "").unwrap();
    until(|| status(d.path(), 0) == "completed");
    assert_eq!(launcher.calls.load(Ordering::SeqCst), 1);
}

struct Fake {
    owner_alive: bool,
}
impl GroupLiveness for Fake {
    fn group_state(&self, _: &str) -> GroupState {
        GroupState::Unknown
    }
    fn process_alive(&self, pid: u32, start: u64) -> bool {
        self.owner_alive && (pid, start) == DAEMON
    }
}
struct Proof(Started);
impl StartProof for Proof {
    fn scope_started(&self, _: &str) -> Started {
        self.0
    }
}
fn record(dir: &Path, id: &str, pid: Option<u32>) {
    let mut r = RunRecord::create(
        dir,
        id,
        "h",
        Default::default(),
        Status::Queued,
        finalize::now(),
    )
    .unwrap();
    if let Some(pid) = pid {
        r.set("pid", json!(pid));
    }
    r.save().unwrap();
}
fn bound(id: &str, group: String) -> ActiveSlot {
    ActiveSlot {
        run_id: id.into(),
        pid: DAEMON.0,
        start_time: DAEMON.1,
        role: Role::Implement,
        class: SlotClass::Writer,
        repo_id: "repo".into(),
        seat_slot: None,
        group: Some(group),
        reservation_mb: 512,
        created_ms: 1,
        bound: true,
    }
}
fn ticket(id: &str) -> Ticket {
    Ticket {
        run_id: id.into(),
        pid: DAEMON.0,
        start_time: DAEMON.1,
        enqueued_ms: 1,
        role: Role::Implement,
        class: SlotClass::Writer,
        repo_id: "repo".into(),
        reservation_mb: 512,
    }
}
fn seed(dir: &Path, rows: Vec<ActiveSlot>, tickets: Vec<Ticket>) {
    slots::with_table(dir, Duration::from_secs(2), |t| {
        t.active = rows;
        t.queued = tickets;
    })
    .unwrap();
}

#[test]
fn restart_releases_a_proven_pre_exec_bind_and_its_ticket() {
    let d = tempfile::tempdir().unwrap();
    record(d.path(), "run-a", None);
    record(d.path(), "run-b", None);
    seed(
        d.path(),
        vec![bound("run-a", unit_name("run-a"))],
        vec![ticket("run-b")],
    );
    let freed =
        release_pre_exec(d.path(), &Fake { owner_alive: false }, &Proof(Started::No)).unwrap();
    assert_eq!(freed, ["run-a", "run-b"]);
    assert_eq!(table(d.path()), SlotTable::default());
}

#[test]
fn restart_keeps_a_live_started_or_unverifiable_binding() {
    let d = tempfile::tempdir().unwrap();
    record(d.path(), "run-a", None);
    seed(
        d.path(),
        vec![bound("run-a", unit_name("run-a"))],
        vec![ticket("run-a2")],
    );
    let before = table(d.path());
    let keeps = |live: bool, proof: Started| {
        let freed = release_pre_exec(d.path(), &Fake { owner_alive: live }, &Proof(proof)).unwrap();
        assert!(
            freed.is_empty(),
            "freed {freed:?} for live={live} proof={proof:?}"
        );
        assert_eq!(table(d.path()), before);
    };
    // The owning daemon is alive: it may be mid-launch.
    keeps(true, Started::No);
    // A scope of the run exists, or the probe cannot tell.
    keeps(false, Started::Yes);
    keeps(false, Started::Unknown);
    // The run record names a process: the run started.
    record(d.path(), "run-a", Some(1234));
    keeps(false, Started::No);
    // No record at all is no proof (the ticket of run-a2 has none either).
    std::fs::remove_dir_all(d.path().join("runs")).unwrap();
    keeps(false, Started::No);
}

#[test]
fn restart_keeps_a_row_already_keyed_by_a_launched_process() {
    let d = tempfile::tempdir().unwrap();
    record(d.path(), "run-a", None);
    let mut row = bound("run-a", "/sys/fs/cgroup/pg-run-a.scope".into());
    row.pid = 4242;
    seed(d.path(), vec![row], vec![]);
    let before = table(d.path());
    let freed =
        release_pre_exec(d.path(), &Fake { owner_alive: false }, &Proof(Started::No)).unwrap();
    assert!(freed.is_empty());
    assert_eq!(table(d.path()), before);
}
