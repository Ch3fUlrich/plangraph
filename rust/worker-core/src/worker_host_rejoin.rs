//! The onboarding gate of a worker host that was unreachable: it stays `rejoining`
//! until every step below has run and one rejoin record is written.
use super::{Phase, State, Store};
use crate::run::record_io::{atomic_write_private, ensure_private_dir};
use serde::Serialize;
use serde_json::{json, Value};
use std::{collections::BTreeSet, fmt, io};

/// What became of a run the coordinator marked unknown during the outage.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RunFate {
    /// Ended while unreachable: collect the result and report it.
    Finished,
    /// Still running: re-attach to its session.
    Live,
    /// Gone without a result: release its claim and lane.
    Dead,
}

#[derive(Debug, Default, Serialize)]
pub struct SyncReport {
    pub head: String,
    /// Checkouts that were fast-forwarded to origin/main.
    pub fast_forwarded: Vec<String>,
    /// Branches that kept local changes saved before the fast-forward.
    pub saved_branches: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct InstallReport {
    /// The installed pg-spawnd and hooks equal the build of main.
    pub current: bool,
    pub detail: String,
}

#[derive(Debug, Serialize)]
pub struct FloorReport {
    pub disk_ok: bool,
    pub ram_ok: bool,
    pub detail: String,
    /// Floors that could not be measured on this host; each becomes a flag.
    #[serde(default)]
    pub unchecked: Vec<String>,
}

#[derive(Debug, Default, Serialize)]
pub struct ReapReport {
    pub removed: u64,
    /// Eligible stale worktrees still present (awaiting approval or not removed).
    pub remaining: u64,
}

#[derive(Debug, Default, Serialize)]
pub struct LegReport {
    pub working: Vec<String>,
    pub failed: Vec<String>,
}

// A report with no leg at all means none is configured for this host: flagged, not blocked.

/// The actions of one rejoin, each run on or for the host being onboarded.
pub trait RejoinHost {
    /// Run the host's code sync: checkouts fast-forwarded, changed ones saved first.
    fn sync_checkouts(&mut self) -> io::Result<SyncReport>;
    /// Compare the installed pg-spawnd and hooks with main.
    fn check_install(&mut self) -> io::Result<InstallReport>;
    fn classify_run(&mut self, run: &str) -> io::Result<RunFate>;
    /// Collect and report, re-attach, or release, as the fate says.
    fn settle_run(&mut self, run: &str, fate: RunFate) -> io::Result<()>;
    /// Run the host's approved worktree reaper and report what it removed and what
    /// stays eligible.
    fn reap_worktrees(&mut self) -> io::Result<ReapReport>;
    fn check_floors(&mut self) -> io::Result<FloorReport>;
    fn probe_legs(&mut self) -> io::Result<LegReport>;
}

#[derive(Debug)]
pub enum RejoinError {
    /// The host is not in a state the gate can lift (ready, unreachable or in maintenance).
    NotRejoining(&'static str),
    /// A step failed; the host stays `rejoining`. Names the step and the reason.
    Blocked {
        step: &'static str,
        reason: String,
    },
    Io(io::Error),
}

impl fmt::Display for RejoinError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotRejoining(reason) => write!(f, "{reason}"),
            Self::Blocked { step, reason } => write!(f, "rejoin blocked at {step}: {reason}"),
            Self::Io(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for RejoinError {}

impl From<io::Error> for RejoinError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

struct Log {
    steps: Vec<Value>,
    flags: Vec<String>,
}

impl Log {
    fn step(&mut self, step: &'static str, status: &str, detail: Value) {
        self.steps
            .push(json!({"step": step, "status": status, "detail": detail}));
    }
}

fn blocked(step: &'static str, reason: impl Into<String>) -> RejoinError {
    RejoinError::Blocked {
        step,
        reason: reason.into(),
    }
}

impl Store {
    /// Run the onboarding gate. On success the rejoin record is written, the
    /// unknown runs are settled and the host is `ready`; on a blocked step the
    /// record of the attempt is written and the host stays `rejoining`.
    pub fn rejoin(&self, host: &mut dyn RejoinHost, now: u64) -> Result<Value, RejoinError> {
        {
            let state = self.load()?;
            match state.phase {
                Phase::Ready => return Err(RejoinError::NotRejoining("host is not rejoining")),
                Phase::Unreachable => return Err(RejoinError::NotRejoining("host is unreachable")),
                Phase::Rejoining if state.maintenance_until.is_some_and(|end| end > now) => {
                    return Err(RejoinError::NotRejoining("host is in a maintenance window"))
                }
                Phase::Rejoining => {}
            }
        }
        let mut log = Log {
            steps: Vec::new(),
            flags: Vec::new(),
        };
        let mut runs = Vec::new();
        let episode = self.load()?.outage_episode;
        let outcome = self.gate(host, &mut log, &mut runs);
        let settled: BTreeSet<String> = runs
            .iter()
            .filter_map(|row| row["run"].as_str().map(str::to_owned))
            .collect();
        // The commit runs under the probe loop's lock: a probe that landed during the
        // gate is read here, so the receipt always agrees with the phase it commits.
        let _guard = self.lock_wait()?;
        let mut state = self.load()?;
        let outcome = outcome.and_then(|()| Self::committable(&state, now, episode, &settled));
        let result = match &outcome {
            Ok(()) => json!({"outcome": "ready"}),
            Err(RejoinError::Blocked { step, reason }) => {
                json!({"outcome": "blocked", "step": step, "reason": reason})
            }
            Err(error) => json!({"outcome": "error", "reason": error.to_string()}),
        };
        let record = json!({
            "host": self.host, "at": now, "result": result,
            "steps": log.steps, "flags": log.flags, "runs": runs,
        });
        let dir = self.root.join("rejoin");
        ensure_private_dir(&dir)?;
        let name = format!(
            "{}-{now}-{}.json",
            self.host,
            if outcome.is_ok() { "ready" } else { "blocked" }
        );
        atomic_write_private(
            &dir.join(name),
            &serde_json::to_vec_pretty(&record).map_err(io::Error::from)?,
        )?;
        outcome?;
        state.needs_rejoin = false;
        state.phase = Phase::Ready;
        state.link.unknown_runs.retain(|run| !settled.contains(run));
        state.link.maintenance_end = None;
        self.save(&state)?;
        Ok(record)
    }

    /// The host may go ready only while it is still reachable, rejoining and outside
    /// a maintenance window. A newer healthy probe does not stop it.
    fn committable(
        state: &State,
        now: u64,
        episode: u64,
        settled: &BTreeSet<String>,
    ) -> Result<(), RejoinError> {
        if state.outage_episode != episode {
            return Err(RejoinError::NotRejoining(
                "a new outage began during the gate",
            ));
        }
        if let Some(run) = state
            .link
            .unknown_runs
            .iter()
            .find(|run| !settled.contains(*run))
        {
            return Err(RejoinError::Blocked {
                step: "runs",
                reason: format!("{run} became unknown during the gate and is not settled"),
            });
        }
        if state.phase != Phase::Rejoining || state.outage_since.is_some() {
            return Err(RejoinError::NotRejoining(
                "host left the rejoining state or went unreachable during the gate",
            ));
        }
        if state.maintenance_until.is_some_and(|end| end > now) {
            return Err(RejoinError::NotRejoining("host is in a maintenance window"));
        }
        Ok(())
    }

    fn lock_wait(&self) -> io::Result<crate::lockfile::LockGuard> {
        for _ in 0..50 {
            match self.lock("lock") {
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    std::thread::sleep(std::time::Duration::from_millis(100));
                }
                other => return other,
            }
        }
        self.lock("lock")
    }

    fn gate(
        &self,
        host: &mut dyn RejoinHost,
        log: &mut Log,
        runs: &mut Vec<Value>,
    ) -> Result<(), RejoinError> {
        let sync = host
            .sync_checkouts()
            .map_err(|error| blocked("sync", error.to_string()))?;
        log.step("sync", "ok", json!(sync));
        let install = host
            .check_install()
            .map_err(|error| blocked("install", error.to_string()))?;
        if install.current {
            log.step("install", "ok", json!(install));
        } else {
            log.flags.push(format!("binary: {}", install.detail));
            log.step("install", "flagged", json!(install));
        }
        let unknown: BTreeSet<String> = self.load()?.link.unknown_runs;
        for run in &unknown {
            let fate = host
                .classify_run(run)
                .map_err(|error| blocked("runs", format!("{run}: {error}")))?;
            host.settle_run(run, fate)
                .map_err(|error| blocked("runs", format!("{run}: {error}")))?;
            runs.push(json!({"run": run, "fate": fate}));
        }
        log.step("runs", "ok", json!({"settled": unknown.len()}));
        let reap = host
            .reap_worktrees()
            .map_err(|error| blocked("reap", error.to_string()))?;
        if reap.remaining > 0 {
            log.step("reap", "failed", json!(reap));
            return Err(blocked(
                "reap",
                format!("{} eligible stale worktrees remain", reap.remaining),
            ));
        }
        log.step("reap", "ok", json!(reap));
        let floors = host
            .check_floors()
            .map_err(|error| blocked("floors", error.to_string()))?;
        if !floors.unchecked.is_empty() {
            let reason = format!("floor not measured: {}", floors.unchecked.join("; "));
            log.step("floors", "failed", json!(floors));
            return Err(blocked("floors", reason));
        }
        if !(floors.disk_ok && floors.ram_ok) {
            let reason = floors.detail.clone();
            log.step("floors", "failed", json!(floors));
            return Err(blocked("floors", reason));
        }
        log.step("floors", "ok", json!(floors));
        let legs = host
            .probe_legs()
            .map_err(|error| blocked("legs", error.to_string()))?;
        if legs.working.is_empty() && legs.failed.is_empty() {
            log.step("legs", "failed", json!(legs));
            return Err(blocked(
                "legs",
                "no leg probes are configured for this host",
            ));
        }
        if legs.working.is_empty() {
            let reason = format!("no leg answered; failed: {}", legs.failed.join(", "));
            log.step("legs", "failed", json!(legs));
            return Err(blocked("legs", reason));
        }
        if !legs.failed.is_empty() {
            log.flags
                .push(format!("legs failing: {}", legs.failed.join(", ")));
        }
        log.step("legs", "ok", json!(legs));
        Ok(())
    }

    /// True when the host stays out of placement until the rejoin gate passes.
    pub fn needs_rejoin(&self) -> io::Result<bool> {
        self.load().map(|state| state.needs_rejoin)
    }
}
