//! Durable private git configuration snapshots; never expose their contents.
use pg_worker_core::run::{
    record::{RunRecord, Status},
    record_io::atomic_write_private,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    fs, io,
    path::{Path, PathBuf},
};

#[derive(Serialize, Deserialize)]
struct Snapshot {
    path: PathBuf,
    bytes: Option<Vec<u8>>,
}

fn optional(path: &Path) -> io::Result<Option<Vec<u8>>> {
    match fs::read(path) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e),
    }
}

// Classify Windows absolute paths even when testing on Unix. Do not join a
// drive path onto a Unix cwd, nor translate WSL mounts into drive paths.
fn resolve(base: &Path, text: &str) -> PathBuf {
    let text = text.trim().replace('\\', "/");
    let b = text.as_bytes();
    if text.starts_with('/')
        || (b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && b[2] == b'/')
    {
        PathBuf::from(text)
    } else {
        base.join(text)
    }
}

pub(crate) fn capture(cwd: &Path, run_dir: &Path) -> io::Result<bool> {
    let dot = cwd.join(".git");
    let mut paths = Vec::new();
    if dot.is_dir() {
        paths.push(dot.join("config"));
    } else if let Some(bytes) = optional(&dot)? {
        let text =
            std::str::from_utf8(&bytes).map_err(|_| io::Error::other("invalid gitdir pointer"))?;
        let pointer = text
            .trim()
            .strip_prefix("gitdir:")
            .ok_or_else(|| io::Error::other("invalid gitdir pointer"))?;
        let gitdir = resolve(cwd, pointer);
        let common = fs::read_to_string(gitdir.join("commondir"))?;
        paths.push(resolve(&gitdir, &common).join("config"));
        paths.push(gitdir.join("config"));
    } else {
        return Ok(false);
    }
    let snapshots = paths
        .into_iter()
        .map(|path| {
            Ok(Snapshot {
                bytes: optional(&path)?,
                path,
            })
        })
        .collect::<io::Result<Vec<_>>>()?;
    let bytes = serde_json::to_vec(&snapshots)
        .map_err(|_| io::Error::other("cannot encode gitconfig snapshot"))?;
    atomic_write_private(&run_dir.join("gitconfig.snapshot"), &bytes)?;
    Ok(true)
}

pub(crate) fn enforce(record: &mut RunRecord, outcome: Status) -> Status {
    if record.get("gitconfig_guard") != Some(&json!(true)) {
        return outcome;
    }
    let result = restore(&record.path().with_file_name("gitconfig.snapshot"));
    let (paths, error) = match result {
        Ok((paths, false)) if paths.is_empty() => return outcome,
        Ok(result) => result,
        Err(()) => (Vec::new(), true),
    };
    // Keep the original result, including its diagnostic, separate from the
    // security outcome. Finalize's subsequent terminal transition is a no-op.
    record.set(
        "gitconfig_original_outcome",
        json!({"status":outcome.as_str(),
        "reason":record.get("reason"), "exit_code":record.get("exit_code")}),
    );
    record.set("reason", json!("gitconfig_tampered"));
    record.set("gitconfig_changed_paths", json!(paths));
    record.set("gitconfig_restore_error", json!(error));
    record.set("status", json!("failed"));
    record.set("exit_code", json!(1));
    record.set("ended", json!(pg_worker_core::run::finalize::now()));
    let _ = record.push_event(json!({"event":"gitconfig_tampered", "paths":paths,
        "restore_error":error}));
    Status::Failed
}

fn restore(sidecar: &Path) -> Result<(Vec<PathBuf>, bool), ()> {
    let snapshots: Vec<Snapshot> =
        serde_json::from_slice(&fs::read(sidecar).map_err(|_| ())?).map_err(|_| ())?;
    let mut changed = Vec::new();
    let mut failed = false;
    for snapshot in snapshots {
        if optional(&snapshot.path).ok().as_ref() == Some(&snapshot.bytes) {
            continue;
        }
        let result = match snapshot.bytes {
            Some(bytes) => atomic_write_private(&snapshot.path, &bytes),
            None => match fs::remove_file(&snapshot.path) {
                Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
                result => result,
            },
        };
        failed |= result.is_err();
        changed.push(snapshot.path);
    }
    Ok((changed, failed))
}

#[cfg(test)]
#[path = "gitconfig_guard_tests.rs"]
mod tests;
