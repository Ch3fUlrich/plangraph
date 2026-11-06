//! Locked, disposable evidence. Never used to make routing decisions.
use crate::evidence_row::{self, EvidenceRow, ObservationRow, OutcomeRow, Phase};
use pg_worker_core::{lockfile, run::record_io};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
#[derive(Debug)]
pub(crate) enum EvidenceError {
    Invalid,
    Io,
    Lock,
}
impl std::fmt::Display for EvidenceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Invalid => "invalid evidence",
            Self::Io => "evidence I/O failed",
            Self::Lock => "evidence lock failed",
        })
    }
}
impl From<std::io::Error> for EvidenceError {
    fn from(_: std::io::Error) -> Self {
        Self::Io
    }
}
pub(crate) struct EvidenceStore {
    dir: PathBuf,
    enabled: bool,
}
#[cfg_attr(
    not(test),
    expect(dead_code, reason = "RL18 consumes the outcome and observation window")
)]
pub(crate) struct EvidenceWindow {
    pub outcomes: Vec<OutcomeRow>,
    pub observations: Vec<ObservationRow>,
}
pub(crate) fn regular(path: &Path, directory: bool) -> Result<(), EvidenceError> {
    match fs::symlink_metadata(path) {
        Ok(m)
            if !m.file_type().is_symlink() && if directory { m.is_dir() } else { m.is_file() } =>
        {
            Ok(())
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        _ => Err(EvidenceError::Invalid),
    }
}
impl EvidenceStore {
    pub fn open(state: &Path, enabled: bool) -> Result<Self, EvidenceError> {
        let store = Self {
            dir: state.join("evidence"),
            enabled,
        };
        if enabled {
            regular(&store.dir, true)?;
            record_io::ensure_private_dir(&store.dir)?;
        }
        Ok(store)
    }
    fn locked(&self) -> Result<lockfile::LockGuard, EvidenceError> {
        regular(&self.dir, true)?;
        let path = self.dir.join("rows.lock");
        regular(&path, false)?;
        // Create the lock privately before the cross-platform lock primitive opens it.
        let mut options = fs::OpenOptions::new();
        options.write(true).create(true).truncate(false);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
        }
        options.open(&path)?;
        let start = Instant::now();
        loop {
            if let Some(guard) = lockfile::try_lock(&path).map_err(|_| EvidenceError::Lock)? {
                return Ok(guard);
            }
            if start.elapsed() >= Duration::from_secs(10) {
                return Err(EvidenceError::Lock);
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    fn read(&self) -> Result<(Vec<EvidenceRow>, u64), EvidenceError> {
        let path = self.dir.join("rows.jsonl");
        regular(&path, false)?;
        let bytes = match fs::read(path) {
            Ok(b) => b,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
            Err(e) => return Err(e.into()),
        };
        let end = bytes.iter().rposition(|b| *b == b'\n').map_or(0, |i| i + 1);
        let mut rows = Vec::new();
        for line in bytes[..end].split_inclusive(|b| *b == b'\n') {
            if line.len() > 65536 {
                return Err(EvidenceError::Invalid);
            }
            let value: Value = serde_json::from_slice(line).map_err(|_| EvidenceError::Invalid)?;
            if value["version"] != 1 {
                return Err(EvidenceError::Invalid);
            }
            let row = serde_json::from_value(value["data"].clone())
                .map_err(|_| EvidenceError::Invalid)?;
            evidence_row::validate(&row)?;
            rows.push(row);
        }
        Ok((rows, end as u64))
    }
    pub fn append(&self, row: &EvidenceRow) -> Result<bool, EvidenceError> {
        self.append_using(row, true)
    }
    fn append_using(&self, row: &EvidenceRow, once: bool) -> Result<bool, EvidenceError> {
        if !self.enabled {
            return Ok(false);
        }
        evidence_row::validate(row)?;
        let _guard = self.locked()?;
        let (rows, end) = self.read()?;
        if once {
            if let EvidenceRow::Outcome(o) = row {
                let latest = rows.iter().rev().find(|r| matches!(r, EvidenceRow::Outcome(old) if old.run_id == o.run_id && old.attempt == o.attempt));
                if latest.and_then(|r| serde_json::to_value(r).ok())
                    == serde_json::to_value(row).ok()
                {
                    return Ok(false);
                }
            }
        }
        if let EvidenceRow::Observation(o) = row {
            let key = o.key()?;
            if rows.iter().any(|r| matches!(r, EvidenceRow::Observation(old) if old.key().ok().as_ref() == Some(&key))) { return Ok(false); }
        }
        let mut bytes = serde_json::to_vec(&json!({"version":1,"data":row}))
            .map_err(|_| EvidenceError::Invalid)?;
        bytes.push(b'\n');
        if bytes.len() > 65536 {
            return Err(EvidenceError::Invalid);
        }
        let path = self.dir.join("rows.jsonl");
        regular(&path, false)?;
        let mut options = fs::OpenOptions::new();
        options.write(true).create(true).truncate(false);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
        }
        let mut file = options.open(path)?;
        file.set_len(end)?;
        use std::io::{Seek, SeekFrom};
        file.seek(SeekFrom::End(0))?;
        file.write_all(&bytes)?;
        file.flush()?;
        file.sync_all()?;
        Ok(true)
    }
    #[cfg_attr(
        not(test),
        expect(dead_code, reason = "RL18 consumes the outcome and observation window")
    )]
    pub fn window(&self, since: i64, until: i64) -> Result<EvidenceWindow, EvidenceError> {
        let mut result = EvidenceWindow {
            outcomes: Vec::new(),
            observations: Vec::new(),
        };
        if !self.enabled {
            return Ok(result);
        }
        let _guard = self.locked()?;
        let mut latest = BTreeMap::new();
        for row in self.read()?.0 {
            match row {
                EvidenceRow::Outcome(o) => {
                    latest.insert((o.run_id.clone(), o.attempt), o);
                }
                EvidenceRow::Observation(o) if o.ts >= since && o.ts < until => {
                    result.observations.push(o)
                }
                _ => {}
            }
        }
        result.outcomes = latest
            .into_values()
            .filter(|o| o.ts >= since && o.ts < until)
            .collect();
        Ok(result)
    }
    pub fn backfill(&self, records: &[Value]) -> Result<usize, EvidenceError> {
        let mut count = 0;
        for record in records {
            for row in evidence_row::adapted(record).0 {
                count += usize::from(self.append_using(&row, true)?);
            }
        }
        Ok(count)
    }
    #[cfg_attr(
        not(test),
        expect(dead_code, reason = "RL18 consumes restart episodes")
    )]
    pub fn open_episodes(&self) -> Result<Vec<ObservationRow>, EvidenceError> {
        if !self.enabled {
            return Ok(Vec::new());
        }
        let _guard = self.locked()?;
        let mut episodes = BTreeMap::new();
        for row in self.read()?.0 {
            if let EvidenceRow::Observation(o) = row {
                if o.kind != evidence_row::ObservationKind::OmniRouteSeen {
                    continue;
                }
                if let Some(phase) = &o.phase {
                    let key = (o.leg_key.clone(), o.state.clone(), o.first_seen);
                    if *phase == Phase::First {
                        episodes.insert(key, o);
                    } else {
                        episodes.remove(&key);
                    }
                }
            }
        }
        Ok(episodes.into_values().collect())
    }
}
pub(crate) fn enabled(state: &Path, role: &str) -> bool {
    let config: Value = fs::File::open(state.join("reliability.json"))
        .ok()
        .and_then(|f| serde_json::from_reader(f.take(65537)).ok())
        .unwrap_or(Value::Null);
    let env = std::env::var("PLANGRAPH_RELIABILITY_EVIDENCE").ok();
    matches!(
        env.as_deref()
            .or_else(|| config["roles"][role]["evidence"].as_str())
            .or_else(|| config["evidence"].as_str()),
        Some("on" | "shadow")
    )
}
#[cfg(test)]
#[path = "evidence_phase_tests.rs"]
mod phase_tests;
#[cfg(test)]
#[path = "evidence_tests.rs"]
mod tests;
