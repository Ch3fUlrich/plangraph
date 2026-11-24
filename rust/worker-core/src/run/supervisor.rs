//! Replaceable local FIFO admission and ownership of this daemon's runs.
use super::{
    group::{Identity, Launcher, SystemLauncher},
    slot_bind::{SlotBind, SlotBindCfg},
    supervise::{self, *},
};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Condvar, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

pub const DEFAULT_MAX_RUNS: usize = 3;
pub const MAX_RUNS_LIMIT: usize = 64;
pub const DEFAULT_MAX_QUEUED: usize = 64;
pub const MAX_QUEUED_LIMIT: usize = 1024;
pub struct SupervisorCfg {
    pub run: Config,
    pub max_runs: usize,
    pub max_queued: usize,
    /// Mirror admission into the shared slot table and require the bound reservation before
    /// every launcher call. `None` keeps the process-local queue alone.
    pub slot_bind: Option<SlotBindCfg>,
}
impl SupervisorCfg {
    pub fn new(run: Config) -> Self {
        Self {
            run,
            max_runs: DEFAULT_MAX_RUNS,
            max_queued: DEFAULT_MAX_QUEUED,
            slot_bind: None,
        }
    }
}
#[derive(Debug, thiserror::Error)]
pub enum SupervisorError {
    #[error("invalid local concurrency cap")]
    BadCap,
    #[error("local queue full")]
    QueueFull,
    #[error("run id already started by this supervisor")]
    DuplicateRun,
    #[error(transparent)]
    Start(#[from] StartError),
    #[error("slot bind: {0}")]
    SlotBind(#[from] super::slot_bind::BindError),
}
#[derive(Debug, PartialEq, Eq)]
pub enum CancelOutcome {
    Requested,
    Finished,
    Unknown,
}
#[derive(Debug, PartialEq, Eq)]
pub struct Counts {
    pub total_started: u64,
    pub running: usize,
    pub queued: usize,
}
#[derive(Default)]
struct State {
    held: usize,
    queue: VecDeque<String>,
    max_held_seen: usize,
    total_started: u64,
}
struct Gate {
    state: Arc<(Mutex<State>, Condvar)>,
    max_runs: usize,
    bind: Option<Arc<SlotBind>>,
}
struct Release(Arc<(Mutex<State>, Condvar)>);
impl Drop for Release {
    fn drop(&mut self) {
        let (lock, changed) = &*self.0;
        lock.lock().unwrap().held -= 1;
        changed.notify_all();
    }
}
impl Admission for Gate {
    fn acquire(&self, id: &str, cancelled: &AtomicBool) -> Admit {
        let (lock, changed) = &*self.state;
        if let Some(bind) = &self.bind {
            // The pending entry is durable before the run waits; a table that cannot hold it
            // refuses the run here, ahead of any launcher call.
            if let Err(e) = bind.pending(id, None) {
                lock.lock().unwrap().queue.retain(|key| key != id);
                changed.notify_all();
                return Admit::Refused(e.to_string());
            }
        }
        let mut s = lock.lock().unwrap();
        if !s.queue.iter().any(|key| key == id) {
            s.queue.push_back(id.into());
        }
        loop {
            if cancelled.load(Ordering::SeqCst) {
                s.queue.retain(|key| key != id);
                changed.notify_all();
                drop(s);
                if let Some(bind) = &self.bind {
                    bind.withdraw(id);
                }
                return Admit::Cancelled;
            }
            if s.queue.front().is_some_and(|key| key == id) && s.held < self.max_runs {
                s.queue.pop_front();
                s.held += 1;
                s.max_held_seen = s.max_held_seen.max(s.held);
                changed.notify_all();
                drop(s);
                let local = Release(self.state.clone());
                let Some(bind) = &self.bind else {
                    return Admit::Granted(Permit::new(local));
                };
                // The reservation is bound before the permit exists, so a launcher call can
                // never happen without it; on refusal `local` frees the local place again.
                return match bind.reserve(id) {
                    Ok(reservation) => Admit::Granted(Permit::new((reservation, local))),
                    Err(e) => {
                        bind.withdraw(id);
                        Admit::Refused(e.to_string())
                    }
                };
            }
            s = changed
                .wait_timeout(s, Duration::from_millis(50))
                .unwrap()
                .0;
        }
    }
    fn marks_launch(&self) -> bool {
        self.bind.is_some()
    }
    fn launched(&self, run_id: &str, identity: &Identity) {
        if let (Some(bind), Some(start)) = (&self.bind, identity.start_time) {
            bind.launched(run_id, identity.pid, start, identity.group.as_deref());
        }
    }
}
/// A start is refused when its run would wait beyond `max_queued`: held permits plus
/// reserved places (a reserved run that a free permit will promote is not waiting)
/// already fill `max_runs + max_queued`. `max_queued` 0 = no waiting run.
pub(crate) fn queue_full(held: usize, reserved: usize, max_runs: usize, max_queued: usize) -> bool {
    held + reserved >= max_runs + max_queued
}
pub struct Supervisor {
    runtime: Arc<Runtime>,
    gate: Arc<Gate>,
    runs: Mutex<HashMap<String, RunHandle>>,
    finished: Mutex<HashSet<String>>,
    max_queued: usize,
}
impl Supervisor {
    pub fn new(cfg: SupervisorCfg) -> Result<Self, SupervisorError> {
        Self::with_launcher(cfg, Arc::new(SystemLauncher))
    }
    pub fn with_launcher(
        cfg: SupervisorCfg,
        launcher: Arc<dyn Launcher>,
    ) -> Result<Self, SupervisorError> {
        if !(1..=MAX_RUNS_LIMIT).contains(&cfg.max_runs) || cfg.max_queued > MAX_QUEUED_LIMIT {
            return Err(SupervisorError::BadCap);
        }
        let gate = Arc::new(Gate {
            state: Arc::new((Mutex::new(State::default()), Condvar::new())),
            max_runs: cfg.max_runs,
            bind: cfg.slot_bind.map(SlotBind::new),
        });
        Ok(Self {
            runtime: Runtime::with(cfg.run, gate.clone(), launcher),
            gate,
            runs: Mutex::new(HashMap::new()),
            finished: Mutex::new(HashSet::new()),
            max_queued: cfg.max_queued,
        })
    }
    pub fn start(&self, spec: RunSpec) -> Result<(), SupervisorError> {
        let mut runs = self.runs.lock().unwrap();
        self.sweep(&mut runs);
        let (lock, changed) = &*self.gate.state;
        let mut s = lock.lock().unwrap();
        let id = spec.run_id.clone();
        if runs.contains_key(&id)
            || s.queue.contains(&id)
            || self.finished.lock().unwrap().contains(&id)
        {
            return Err(SupervisorError::DuplicateRun);
        }
        if queue_full(s.held, s.queue.len(), self.gate.max_runs, self.max_queued) {
            return Err(SupervisorError::QueueFull);
        }
        // The pending entry is durable before the start is acknowledged, so a crash after the
        // acknowledgement leaves a ticket that restart recovery sees.
        if let Some(bind) = &self.gate.bind {
            let role = spec
                .base
                .get("role")
                .and_then(|v| serde_json::from_value(v.clone()).ok());
            bind.pending(&id, role)?;
        }
        s.queue.push_back(id.clone());
        match supervise::start(&self.runtime, spec) {
            Ok(handle) => {
                s.total_started += 1;
                runs.insert(id, handle);
                Ok(())
            }
            Err(e) => {
                // Return this reservation when record creation or thread spawn fails.
                if let Some(pos) = s.queue.iter().rposition(|key| key == &id) {
                    s.queue.remove(pos);
                }
                if let Some(bind) = &self.gate.bind {
                    bind.withdraw(&id);
                }
                changed.notify_all();
                Err(SupervisorError::Start(e))
            }
        }
    }
    pub fn cancel(&self, id: &str) -> CancelOutcome {
        let runs = self.runs.lock().unwrap();
        match runs.get(id) {
            Some(h) if h.is_finished() => CancelOutcome::Finished,
            Some(h) => {
                h.cancel();
                CancelOutcome::Requested
            }
            None => {
                // Swept handles are still recognizable as this instance's starts.
                if self.finished.lock().unwrap().contains(id) {
                    CancelOutcome::Finished
                } else {
                    CancelOutcome::Unknown
                }
            }
        }
    }
    pub fn is_owned(&self, id: &str) -> bool {
        let mut runs = self.runs.lock().unwrap();
        self.sweep(&mut runs);
        runs.contains_key(id)
    }
    pub fn counts(&self) -> Counts {
        self.sweep(&mut self.runs.lock().unwrap());
        let s = self.gate.state.0.lock().unwrap();
        Counts {
            total_started: s.total_started,
            running: s.held,
            queued: s.queue.len(),
        }
    }
    pub fn wait_finished(&self, id: &str, timeout: Duration) -> bool {
        let began = Instant::now();
        loop {
            if !self.is_owned(id) {
                return true;
            }
            if began.elapsed() >= timeout {
                return false;
            }
            thread::sleep(Duration::from_millis(20));
        }
    }
    pub fn config(&self) -> &Config {
        &self.runtime.cfg
    }
    #[cfg(all(test, unix))]
    pub(super) fn max_held_seen(&self) -> usize {
        self.gate.state.0.lock().unwrap().max_held_seen
    }
    fn sweep(&self, runs: &mut HashMap<String, RunHandle>) {
        let mut finished = self.finished.lock().unwrap();
        runs.retain(|id, h| {
            if h.is_finished() {
                finished.insert(id.clone());
                false
            } else {
                true
            }
        });
    }
}
