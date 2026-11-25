//! Replaceable local FIFO admission and ownership of this daemon's runs.
use super::{
    group::{Launcher, SystemLauncher},
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
}
impl SupervisorCfg {
    pub fn new(run: Config) -> Self {
        Self {
            run,
            max_runs: DEFAULT_MAX_RUNS,
            max_queued: DEFAULT_MAX_QUEUED,
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
        let mut s = lock.lock().unwrap();
        if !s.queue.iter().any(|key| key == id) {
            s.queue.push_back(id.into());
        }
        loop {
            if cancelled.load(Ordering::SeqCst) {
                s.queue.retain(|key| key != id);
                changed.notify_all();
                return Admit::Cancelled;
            }
            if s.queue.front().is_some_and(|key| key == id) && s.held < self.max_runs {
                s.queue.pop_front();
                s.held += 1;
                s.max_held_seen = s.max_held_seen.max(s.held);
                changed.notify_all();
                return Admit::Granted(Permit::new(Release(self.state.clone())));
            }
            s = changed
                .wait_timeout(s, Duration::from_millis(50))
                .unwrap()
                .0;
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
