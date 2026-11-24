//! The findings producer: the one way an orchestrator hands a fix round to a lane. It
//! writes the reviewed-head sidecar first and the prompt file last, each by rename, so a
//! reader never sees a prompt without its head or a half-written file.

use crate::contract::{findings_name, highest_round, reviewed_head_path};
use std::path::{Path, PathBuf};

fn put(path: &Path, text: &str) -> Result<(), String> {
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, text).map_err(|e| format!("{}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, path).map_err(|e| format!("{}: {e}", path.display()))
}

/// Write the next round of `lane` for `head`; returns the prompt file's path.
pub fn deliver(dir: &Path, lane: &str, head: &str, text: &str) -> Result<PathBuf, String> {
    if lane.is_empty() || lane.contains(['/', '\\']) || lane.starts_with('.') {
        return Err("lane must be a plain name".into());
    }
    if head.len() != 40 || !head.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("head must be a 40-hex sha".into());
    }
    if text.trim().is_empty() {
        return Err("the findings text is empty".into());
    }
    // One producer per lane at a time: the round is allocated and both files published while
    // the lock is held, so no reader sees one review's head next to another review's text.
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(dir.join(format!("{lane}.deliver.lock")))
        .map_err(|e| format!("deliver lock: {e}"))?;
    lock.lock().map_err(|e| format!("deliver lock: {e}"))?;
    let round = highest_round(dir, lane) + 1;
    put(
        &reviewed_head_path(dir, lane, round),
        &head.to_ascii_lowercase(),
    )?;
    let file = dir.join(findings_name(lane, round));
    put(&file, text)?;
    drop(lock);
    Ok(file)
}
