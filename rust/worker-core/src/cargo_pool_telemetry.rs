//! Cargo file-lock wait telemetry: per-run samples, a stderr tracker and per-host summaries.
use crate::run::record_io::append_private;
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, fs, io, path::Path};

const LOCK_WAIT_LOG: &str = "cargo-pool-lock-waits.jsonl";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LockWaitSample {
    pub host: String,
    pub run_id: String,
    pub slot: usize,
    pub build_ms: u64,
    pub lock_wait_ms: u64,
    pub blocking_events: u64,
    pub ts: u64,
}

#[derive(Debug, Default, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LockWaitSummary {
    pub builds: u64,
    pub build_ms: u64,
    pub lock_wait_ms: u64,
    pub blocking_events: u64,
}

impl LockWaitSummary {
    pub fn lock_wait_ratio(&self) -> f64 {
        if self.build_ms == 0 {
            0.0
        } else {
            self.lock_wait_ms as f64 / self.build_ms as f64
        }
    }
}

pub fn count_cargo_lock_waits(stderr: &str) -> u64 {
    stderr.lines().filter(|line| is_blocking(line)).count() as u64
}

pub fn record_lock_wait(state: &Path, sample: &LockWaitSample) -> io::Result<()> {
    fs::create_dir_all(state)?;
    let mut line = serde_json::to_string(sample).map_err(io::Error::other)?;
    line.push('\n');
    append_private(&state.join(LOCK_WAIT_LOG), line.as_bytes())
}

pub fn summarize_lock_waits(state: &Path) -> io::Result<BTreeMap<String, LockWaitSummary>> {
    let path = state.join(LOCK_WAIT_LOG);
    let content = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
        Err(e) => return Err(e),
    };
    let mut summary: BTreeMap<String, LockWaitSummary> = BTreeMap::new();
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let sample: LockWaitSample = serde_json::from_str(trimmed).map_err(io::Error::other)?;
        let entry = summary.entry(sample.host).or_default();
        entry.builds += 1;
        entry.build_ms += sample.build_ms;
        entry.lock_wait_ms += sample.lock_wait_ms;
        entry.blocking_events += sample.blocking_events;
    }
    Ok(summary)
}

const BLOCKING: [&str; 2] = [
    "Blocking waiting for file lock on build directory",
    "Blocking waiting for file lock on artifact directory",
];

fn is_blocking(line: &str) -> bool {
    BLOCKING.iter().any(|m| line.contains(m))
}

/// Measures Cargo lock waits from timestamped stderr lines: a wait starts at a
/// "Blocking waiting for file lock" line and ends at the next other line (Cargo
/// prints its next status line once it holds the lock) or at `finish`.
#[derive(Debug, Default)]
pub struct LockWaitTracker {
    blocking_events: u64,
    lock_wait_ms: u64,
    waiting_since: Option<u64>,
}

impl LockWaitTracker {
    pub fn observe(&mut self, line: &str, at_ms: u64) {
        if is_blocking(line) {
            self.blocking_events += 1;
            self.waiting_since.get_or_insert(at_ms);
        } else if let Some(since) = self.waiting_since.take() {
            self.lock_wait_ms += at_ms.saturating_sub(since);
        }
    }

    /// Close an open wait at `end_ms`; returns `(blocking_events, lock_wait_ms)`.
    pub fn finish(mut self, end_ms: u64) -> (u64, u64) {
        if let Some(since) = self.waiting_since.take() {
            self.lock_wait_ms += end_ms.saturating_sub(since);
        }
        (self.blocking_events, self.lock_wait_ms)
    }
}

/// Name of this host for per-host samples; `unknown` when the kernel does not say.
pub fn host_name() -> String {
    fs::read_to_string("/proc/sys/kernel/hostname")
        .ok()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "unknown".into())
}
