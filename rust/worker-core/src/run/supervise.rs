//! One admitted run; deliberately not wired into placement or a watchdog.
use super::slot_bind::BindError;
use super::{
    finalize,
    group::*,
    record::{valid_run_id, RecordError, RunRecord, Status},
};
use serde_json::{json, Map, Value};
use std::{
    collections::BTreeSet,
    ffi::OsString,
    io,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::Duration,
};
pub type FinishHook = Box<dyn FnOnce(&mut RunRecord, Status) -> Option<RunSpec> + Send>;
pub struct RunSpec {
    pub run_id: String,
    pub job_id: String,
    pub program: OsString,
    pub args: Vec<OsString>,
    pub cwd: PathBuf,
    pub env: Vec<(OsString, OsString)>,
    pub stdin: Option<Vec<u8>>,
    pub base: Map<String, Value>,
    pub events: Vec<Value>,
    pub finish: Option<FinishHook>,
    pub allow_shas: BTreeSet<String>,
}
pub struct Config {
    pub state_dir: PathBuf,
    pub host: String,
    pub confinement: Confinement,
    pub limits: RunLimits,
    pub grace: Duration,
    pub kill_timeout: Duration,
    pub save_every: Duration,
    pub reader_grace: Duration,
    pub held_poll: Duration,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            state_dir: PathBuf::new(),
            host: String::new(),
            confinement: Confinement::default(),
            limits: RunLimits::default(),
            grace: Duration::from_secs(10),
            kill_timeout: Duration::from_secs(10),
            save_every: Duration::from_secs(2),
            reader_grace: Duration::from_secs(5),
            held_poll: Duration::from_secs(1),
        }
    }
}
pub trait Admission: Send + Sync {
    fn acquire(&self, run_id: &str, cancelled: &AtomicBool) -> Admit;
    /// The launcher started `identity` for a run this admission granted.
    fn launched(&self, _run_id: &str, _identity: &Identity) {}
    /// The queued run record exists; persist whatever must be durable before `start` returns
    /// (the monitor thread is not running yet). `base` holds the caller's run fields.
    fn enqueued(&self, _run_id: &str, _base: &Map<String, Value>) -> Result<(), BindError> {
        Ok(())
    }
    /// Take back what [`Self::enqueued`] persisted for a run whose monitor never started.
    fn withdrawn(&self, _run_id: &str) {}
    /// Whether the monitor must persist the launch marker before each launcher call (a
    /// durable record of "the launch began" for restart recovery of bound slots).
    fn marks_launch(&self) -> bool {
        false
    }
}
pub enum Admit {
    Granted(Permit),
    Cancelled,
    /// The grant could not be made durable; the run must not be launched.
    Refused(String),
}
pub struct Permit {
    _guard: Box<dyn Send>,
}
impl Permit {
    pub fn new(guard: impl Send + 'static) -> Self {
        Self {
            _guard: Box::new(guard),
        }
    }
}
pub struct NoLimit;
impl Admission for NoLimit {
    fn acquire(&self, _: &str, _: &AtomicBool) -> Admit {
        Admit::Granted(Permit::new(()))
    }
}
pub struct Runtime {
    pub cfg: Config,
    pub admission: Arc<dyn Admission>,
    pub launcher: Arc<dyn Launcher>,
}
impl Runtime {
    pub fn new(cfg: Config) -> Arc<Self> {
        Self::with(cfg, Arc::new(NoLimit), Arc::new(SystemLauncher))
    }
    pub fn with(
        cfg: Config,
        admission: Arc<dyn Admission>,
        launcher: Arc<dyn Launcher>,
    ) -> Arc<Self> {
        Arc::new(Self {
            cfg,
            admission,
            launcher,
        })
    }
}
pub struct RunHandle {
    id: String,
    cancel: Arc<AtomicBool>,
    thread: JoinHandle<()>,
}
impl RunHandle {
    pub fn run_id(&self) -> &str {
        &self.id
    }
    pub fn cancel(&self) {
        self.cancel.store(true, Ordering::SeqCst);
    }
    pub fn is_finished(&self) -> bool {
        self.thread.is_finished()
    }
    pub fn join(self) -> thread::Result<()> {
        self.thread.join()
    }
}
#[derive(Debug, thiserror::Error)]
pub enum StartError {
    #[error("bad id")]
    BadId,
    #[error("reserved key: {0}")]
    Reserved(String),
    #[error("bad event")]
    BadEvent,
    #[error(transparent)]
    Record(#[from] RecordError),
    #[error(transparent)]
    Spawn(io::Error),
    #[error("admission: {0}")]
    Admission(#[from] BindError),
}
const RESERVED: &[&str] = &[
    "status",
    "started",
    "ended",
    "exit_code",
    "pid",
    "pid_start",
    "job_object",
    "job_id",
    "proc",
    "last_activity_ts",
    "stdout_tail",
    "stderr_tail",
    "events",
    "live_log",
    "queued_at",
    "reason",
    "error",
    "survivors",
    "launch_started",
];
pub fn validate(spec: &RunSpec) -> Result<(), StartError> {
    if !valid_run_id(&spec.run_id)
        || !spec.job_id.strip_prefix("job-").is_some_and(|s| {
            (1..=32).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric())
        })
    {
        return Err(StartError::BadId);
    }
    for &key in RESERVED {
        if spec.base.contains_key(key) {
            return Err(StartError::Reserved(key.into()));
        }
    }
    if spec
        .events
        .iter()
        .any(|e| !e.is_object() || e.get("event").and_then(Value::as_str).is_none())
    {
        return Err(StartError::BadEvent);
    }
    Ok(())
}
pub fn create_record(rt: &Runtime, spec: &mut RunSpec) -> Result<RunRecord, StartError> {
    validate(spec)?;
    let mut record = RunRecord::create(
        &rt.cfg.state_dir,
        &spec.run_id,
        &rt.cfg.host,
        std::mem::take(&mut spec.base),
        Status::Queued,
        finalize::now(),
    )?;
    for e in spec.events.drain(..) {
        record.push_event(e)?;
    }
    record.save()?;
    Ok(record)
}
pub fn launch_error(record: &mut RunRecord, error: LaunchError) {
    let mut detail = None;
    let name = match error {
        LaunchError::Io(e) => format!("{:?}", e.kind()),
        LaunchError::ScopeUnavailable(msg) => {
            detail = Some(msg);
            "ScopeUnavailable".into()
        }
        LaunchError::ScopeNotEntered => "ScopeNotEntered".into(),
        LaunchError::Unsupported => "Unsupported".into(),
        LaunchError::JobUnavailable => "JobUnavailable".into(),
        LaunchError::JobExists => "JobExists".into(),
        LaunchError::BadUnit => "BadUnit".into(),
    };
    let event = if name == "JobUnavailable" || name == "JobExists" {
        json!({"event":"win_job_unavailable"})
    } else {
        json!({"event":"spawn_failed", "error":name})
    };
    let _ = record.push_event(event);
    if let Some(detail) = detail {
        let _ = record.push_event(json!({"event":"scope_unavailable", "detail":detail}));
    }
    record.set("error", json!(format!("spawn failed: {name}")));
    record.set("reason", json!(format!("spawn failed: {name}")));
}

/// A run that never got a monitor: the queued record ends as failed with `reason`.
fn abandon(mut spec: RunSpec, mut record: RunRecord, reason: &str) {
    record.set("error", json!(reason));
    record.set("reason", json!(reason));
    finalize::finish(
        &mut record,
        &mut spec.finish,
        1,
        false,
        StopOutcome::Empty,
        None,
    );
}
pub fn start(rt: &Arc<Runtime>, mut spec: RunSpec) -> Result<RunHandle, StartError> {
    let fields = spec.base.clone();
    let record = create_record(rt, &mut spec)?;
    let id = spec.run_id.clone();
    // The queued record is durable before the ticket, and the ticket before the monitor, so a
    // crash at any point leaves a record that recovery can match to its ticket.
    if let Err(e) = rt.admission.enqueued(&id, &fields) {
        abandon(spec, record, "admission refused");
        return Err(e.into());
    }
    let cancel = Arc::new(AtomicBool::new(false));
    let (runtime, flag) = (rt.clone(), cancel.clone());
    // Keep ownership available to the caller if the OS refuses the thread.
    let pending = Arc::new(Mutex::new(Some((spec, record))));
    let work = pending.clone();
    let thread = thread::Builder::new()
        .name(id.clone())
        .spawn(move || {
            let (spec, record) = work.lock().unwrap().take().unwrap();
            super::monitor::monitor(runtime, spec, record, flag);
        })
        .map_err(|e| {
            if let Some((spec, r)) = pending.lock().unwrap().take() {
                abandon(spec, r, "monitor thread failed");
            }
            rt.admission.withdrawn(&id);
            StartError::Spawn(e)
        })?;
    Ok(RunHandle { id, cancel, thread })
}
