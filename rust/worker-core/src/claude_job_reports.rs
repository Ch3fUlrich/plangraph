//! Report stale terminal Claude job directories for operator review.
use serde::Serialize;
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};

const IDLE: Duration = Duration::from_secs(7 * 24 * 3600);

#[derive(Serialize)]
pub struct Candidate {
    pub path: PathBuf,
    pub bytes: u64,
}

fn terminal_timestamp(path: &Path) -> Option<SystemTime> {
    let state = path.join("state.json");
    if !state.symlink_metadata().ok()?.is_file() {
        return None;
    }
    let state: serde_json::Value = serde_json::from_slice(&fs::read(state).ok()?).ok()?;
    if state["state"] != "done" {
        return None;
    }
    let updated = chrono::DateTime::parse_from_rfc3339(state["updatedAt"].as_str()?).ok()?;
    Some(updated.into())
}

pub fn candidates(root: &Path, now: SystemTime) -> io::Result<Vec<Candidate>> {
    let root = root.canonicalize()?;
    let mut candidates = Vec::new();
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let path = entry.path();
        let Some(terminal) = terminal_timestamp(&path) else {
            continue;
        };
        if now.duration_since(terminal).unwrap_or_default() <= IDLE {
            continue;
        }
        let (bytes, newest) = crate::disk_hygiene::scan(&path)?;
        if now.duration_since(newest).unwrap_or_default() > IDLE {
            candidates.push(Candidate { path, bytes });
        }
    }
    candidates.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(candidates)
}
