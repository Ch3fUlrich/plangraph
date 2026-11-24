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
    /// Whether the run record already carried the launch marker at each call.
    marked_at_call: Mutex<Vec<bool>>,
    /// The role of the bound row of each launched run, by unit.
    role_at_call: Mutex<Vec<(String, Role)>>,
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
        if let Some(row) = table(&self.slots_dir)
            .active
            .iter()
            .find(|s| s.group.as_deref() == Some(unit))
        {
            self.role_at_call
                .lock()
                .unwrap()
                .push((unit.into(), row.role));
        }
        let run_id = unit.strip_prefix("pg-").unwrap();
        let marked = RunRecord::read(&self.slots_dir, run_id)
            .unwrap()
            .get(LAUNCH_STARTED)
            .is_some_and(|v| !v.is_null());
        self.marked_at_call.lock().unwrap().push(marked);
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
        marked_at_call: Mutex::new(Vec::new()),
        role_at_call: Mutex::new(Vec::new()),
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
    // The refusal comes back from `start` itself, before any record is written, with no
    // launcher call.
    assert!(matches!(
        s.start(spec(d.path(), 3, "true")),
        Err(SupervisorError::Start(StartError::Admission(_)))
    ));
    assert_eq!(launcher.calls.load(Ordering::SeqCst), 0);
    assert!(RunRecord::read(d.path(), "run-3").is_err());
    assert!(!d.path().join("runs/run-3").exists());
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
    // The launch marker was durable before the launcher ran.
    assert_eq!(*launcher.marked_at_call.lock().unwrap(), [true]);
}

#[test]
fn an_acknowledged_start_already_has_its_durable_ticket() {
    let d = tempfile::tempdir().unwrap();
    let (s, _launcher) = supervisor(d.path(), 1);
    s.start(spec(d.path(), 0, HOLD)).unwrap();
    until(|| status(d.path(), 0) == "running");
    s.start(spec(d.path(), 1, "true")).unwrap();
    // No waiting: the ticket was written before `start` returned.
    assert!(table(d.path()).queued.iter().any(|t| t.run_id == "run-1"));
    std::fs::write(d.path().join("go"), "").unwrap();
    until(|| status(d.path(), 1) == "completed");
}

/// The queued record exists before the ticket is written, and the ticket before the monitor.
struct Order {
    dir: std::path::PathBuf,
    record_existed: Mutex<Option<bool>>,
}
impl Admission for Order {
    fn enqueued(
        &self,
        run_id: &str,
        _: &serde_json::Map<String, serde_json::Value>,
    ) -> Result<(), BindError> {
        *self.record_existed.lock().unwrap() = Some(RunRecord::read(&self.dir, run_id).is_ok());
        Ok(())
    }
    fn acquire(&self, _: &str, _: &std::sync::atomic::AtomicBool) -> Admit {
        // The monitor runs only after `enqueued` returned.
        assert!(self.record_existed.lock().unwrap().is_some());
        Admit::Cancelled
    }
}
#[test]
fn the_queued_record_is_durable_before_the_ticket_and_the_ticket_before_the_monitor() {
    let d = tempfile::tempdir().unwrap();
    let order = Arc::new(Order {
        dir: d.path().into(),
        record_existed: Mutex::new(None),
    });
    let cfg = Config {
        state_dir: d.path().into(),
        confinement: Confinement::Plain,
        ..Default::default()
    };
    let rt = Runtime::with(cfg, order.clone(), Arc::new(SystemLauncher));
    super::supervise::start(&rt, spec(d.path(), 7, "true"))
        .unwrap()
        .join()
        .unwrap();
    assert_eq!(*order.record_existed.lock().unwrap(), Some(true));
}

#[test]
fn restart_frees_a_ticket_whose_record_was_created_first_and_never_started() {
    let d = tempfile::tempdir().unwrap();
    // The window after the ticket write: a queued record, a ticket, a dead daemon.
    record(d.path(), "run-w", None);
    let mut review = ticket("run-w");
    review.role = Role::Review;
    let mut row = bound("run-r", unit_name("run-r"));
    row.role = Role::Review;
    record(d.path(), "run-r", None);
    seed(d.path(), vec![row], vec![review]);
    let freed =
        release_pre_exec(d.path(), &Fake { owner_alive: false }, &Proof(Started::No)).unwrap();
    assert_eq!(freed, ["run-r", "run-w"]);
    assert_eq!(table(d.path()), SlotTable::default());
}

#[test]
fn the_ticket_and_the_reservation_carry_the_role_of_the_run() {
    let d = tempfile::tempdir().unwrap();
    let (s, launcher) = supervisor(d.path(), 1);
    s.start(spec(d.path(), 0, HOLD)).unwrap();
    until(|| status(d.path(), 0) == "running");
    let mut review = spec(d.path(), 1, "true");
    review.base.insert("role".into(), json!("review"));
    s.start(review).unwrap();
    let t = table(d.path());
    assert_eq!(
        t.queued.iter().find(|t| t.run_id == "run-1").unwrap().role,
        Role::Review
    );
    assert_eq!(
        t.active.iter().find(|a| a.run_id == "run-0").unwrap().role,
        Role::Implement
    );
    std::fs::write(d.path().join("go"), "").unwrap();
    until(|| status(d.path(), 1) == "completed");
    // The promoted review run held a bound row under its own role at its launcher call.
    assert_eq!(
        *launcher.role_at_call.lock().unwrap(),
        [
            ("pg-run-0".to_owned(), Role::Implement),
            ("pg-run-1".to_owned(), Role::Review)
        ]
    );
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
    // A record that carries the launch marker: the launcher may have spawned a process.
    let mut marked = RunRecord::read(d.path(), "run-a").unwrap();
    marked.set("pid", serde_json::Value::Null);
    marked.set(LAUNCH_STARTED, json!(1));
    marked.save().unwrap();
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

#[test]
fn a_start_for_an_id_that_already_has_a_run_leaves_that_run_untouched() {
    let d = tempfile::tempdir().unwrap();
    let mut existing = RunRecord::create(
        d.path(),
        "run-5",
        "h",
        Default::default(),
        Status::Running,
        finalize::now(),
    )
    .unwrap();
    existing.set("pid", json!(4242));
    existing.save().unwrap();
    let before = RunRecord::read(d.path(), "run-5").unwrap().to_value();
    let (s, launcher) = supervisor(d.path(), 2);
    assert!(matches!(
        s.start(spec(d.path(), 5, "true")),
        Err(SupervisorError::Start(StartError::Admission(
            BindError::Exists
        )))
    ));
    assert_eq!(
        RunRecord::read(d.path(), "run-5").unwrap().to_value(),
        before
    );
    assert_eq!(launcher.calls.load(Ordering::SeqCst), 0);
    assert_eq!(table(d.path()), SlotTable::default());
}

#[test]
fn the_queued_record_names_the_daemon_that_created_it() {
    let d = tempfile::tempdir().unwrap();
    let (s, _launcher) = supervisor(d.path(), 1);
    s.start(spec(d.path(), 0, HOLD)).unwrap();
    until(|| status(d.path(), 0) == "running");
    let v = RunRecord::read(d.path(), "run-0").unwrap().to_value();
    assert_eq!(
        v[SLOT_OWNER],
        json!({"pid": DAEMON.0, "start_time": DAEMON.1})
    );
    std::fs::write(d.path().join("go"), "").unwrap();
    until(|| status(d.path(), 0) == "completed");
}

fn owned_queued_record(dir: &Path, id: &str, owner: Option<(u32, u64)>) {
    let base = owner
        .map(|(pid, start)| json!({SLOT_OWNER: {"pid": pid, "start_time": start}}))
        .map(|v| v.as_object().unwrap().clone())
        .unwrap_or_default();
    RunRecord::create(dir, id, "h", base, Status::Queued, finalize::now()).unwrap();
}

#[test]
fn restart_settles_a_queued_record_a_dead_daemon_abandoned_before_its_ticket() {
    let d = tempfile::tempdir().unwrap();
    // No ticket and no slot row: the crash came between the record and the ticket.
    owned_queued_record(d.path(), "run-lost", Some(DAEMON));
    // Records that must stay: another owner, no owner, a launch that began.
    owned_queued_record(d.path(), "run-other", Some((99, 5)));
    owned_queued_record(d.path(), "run-plain", None);
    owned_queued_record(d.path(), "run-began", Some(DAEMON));
    let mut began = RunRecord::read(d.path(), "run-began").unwrap();
    began.set(LAUNCH_STARTED, json!(1));
    began.save().unwrap();
    struct OnlyDaemonDead;
    impl GroupLiveness for OnlyDaemonDead {
        fn group_state(&self, _: &str) -> GroupState {
            GroupState::Unknown
        }
        fn process_alive(&self, pid: u32, _: u64) -> bool {
            pid != DAEMON.0
        }
    }
    let settled = release_pre_exec(d.path(), &OnlyDaemonDead, &Proof(Started::No)).unwrap();
    assert_eq!(settled, ["run-lost"]);
    let state = |id: &str| RunRecord::read(d.path(), id).unwrap().to_value()["status"].clone();
    assert_eq!(state("run-lost"), "failed");
    for kept in ["run-other", "run-plain", "run-began"] {
        assert_eq!(state(kept), "queued", "{kept}");
    }
}
