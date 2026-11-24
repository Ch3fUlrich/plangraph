//! The onboarding gate of a worker host that was unreachable: it stays `rejoining`
//! until every step below has run and one rejoin record is written.
use super::{Phase, Store};
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
    /// Reap stale worktrees; returns how many were removed.
    fn reap_worktrees(&mut self) -> io::Result<u64>;
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
        let outcome = self.gate(host, &mut log, &mut runs);
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
        self.mark_ready(now)?;
        Ok(record)
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
        let reaped = host
            .reap_worktrees()
            .map_err(|error| blocked("reap", error.to_string()))?;
        log.step("reap", "ok", json!({"reaped": reaped}));
        let floors = host
            .check_floors()
            .map_err(|error| blocked("floors", error.to_string()))?;
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
            log.flags.push("legs: none configured to probe".into());
        } else if legs.working.is_empty() {
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

    fn mark_ready(&self, now: u64) -> Result<(), RejoinError> {
        let _guard = self.lock("lock")?;
        let mut state = self.load()?;
        // The probe loop may have lost the host again while the gate ran.
        if state.phase != Phase::Rejoining {
            return Err(RejoinError::NotRejoining(
                "host left the rejoining state during the gate",
            ));
        }
        if now < state.observed_at {
            return Err(io::Error::other("worker host observation clock moved backwards").into());
        }
        state.needs_rejoin = false;
        state.phase = Phase::Ready;
        state.link.unknown_runs.clear();
        state.link.maintenance_end = None;
        self.save(&state)?;
        Ok(())
    }

    /// True when the host stays out of placement until the rejoin gate passes.
    pub fn needs_rejoin(&self) -> io::Result<bool> {
        self.load().map(|state| state.needs_rejoin)
    }
}
