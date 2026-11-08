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

fn normalized(path: &Path) -> String {
    let mut text = path.to_string_lossy().replace('\\', "/");
    text = text.strip_prefix("//?/").unwrap_or(&text).to_owned();
    let b = text.as_bytes();
    if b.len() > 2 && b[0].is_ascii_alphabetic() && b[1] == b':' {
        text = format!("/mnt/{}{}", (b[0] as char).to_ascii_lowercase(), &text[2..]);
    }
    let absolute = text.starts_with('/');
    let floor = if text.as_bytes().get(6) == Some(&b'/') && text.starts_with("/mnt/") {
        2
    } else {
        0
    };
    let mut parts = Vec::new();
    for part in text.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if parts.len() > floor && parts.last().is_some_and(|p| *p != "..") {
                    parts.pop();
                } else if !absolute {
                    parts.push(part);
                }
            }
            _ => parts.push(part),
        }
    }
    format!("{}{}", if absolute { "/" } else { "" }, parts.join("/"))
}

fn same_resolved_path(left: &Path, right: &Path) -> bool {
    let left = normalized(left);
    let right = normalized(right);
    let windows = |p: &str| {
        p.strip_prefix("/mnt/").is_some_and(|tail| {
            let b = tail.as_bytes();
            b.len() >= 2 && b[0].is_ascii_alphabetic() && b[1] == b'/'
        })
    };
    if windows(&left) || windows(&right) {
        left.eq_ignore_ascii_case(&right)
    } else {
        left == right
    }
}

pub(crate) fn capture(cwd: &Path, run_dir: &Path) -> io::Result<bool> {
    let cwd = fs::canonicalize(cwd)?;
    let dot = cwd.join(".git");
    safe(&dot)?;
    let mut paths = Vec::new();
    if dot.is_dir() {
        paths.push(fs::canonicalize(&dot)?.join("config"));
    } else if let Some(bytes) = optional(&dot)? {
        let text =
            std::str::from_utf8(&bytes).map_err(|_| io::Error::other("invalid gitdir pointer"))?;
        let pointer = text
            .trim()
            .strip_prefix("gitdir:")
            .ok_or_else(|| io::Error::other("invalid gitdir pointer"))?;
        let gitdir = resolve(&cwd, pointer);
        let gitdir = fs::canonicalize(gitdir)?;
        safe(&gitdir.join("commondir"))?;
        match fs::read_to_string(gitdir.join("commondir")) {
            Ok(text) => {
                let common = resolve(&gitdir, &text);
                let common = fs::canonicalize(common)?;
                if !common.join("HEAD").is_file()
                    || !common.join("config").is_file()
                    || gitdir.parent() != Some(common.join("worktrees").as_path())
                {
                    return Err(io::Error::other("gitconfig_untrusted_gitdir"));
                }
                // Git's backlink binds this administrative directory to this checkout.
                let backlink = fs::read_to_string(gitdir.join("gitdir"))?;
                let backlink = fs::canonicalize(resolve(&gitdir, &backlink))?;
                let dot = fs::canonicalize(&dot)?;
                if !same_resolved_path(&backlink, &dot) {
                    return Err(io::Error::other("gitconfig_untrusted_gitdir"));
                }
                paths.push(common.join("config"));
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                // Submodules have no commondir. Only accept the enclosing repo's modules.
                if !cwd.ancestors().skip(1).any(|p| {
                    let root = p.join(".git/modules");
                    root.is_dir() && gitdir.starts_with(root)
                }) {
                    return Err(io::Error::other("gitconfig_untrusted_gitdir"));
                }
            }
            Err(e) => return Err(e),
        }
        paths.push(gitdir.join("config"));
    } else {
        return Ok(false);
    }
    let worktree_paths: Vec<_> = paths
        .iter()
        .map(|p| p.with_file_name("config.worktree"))
        .collect();
    paths.extend(worktree_paths);
    let snapshots = paths
        .into_iter()
        .map(|path| {
            safe(&path)?;
            Ok(Snapshot {
                bytes: optional(&path)?,
                path,
            })
        })
        .collect::<io::Result<Vec<_>>>()?;
    let bytes = serde_json::to_vec(&snapshots)
        .map_err(|_| io::Error::other("cannot encode gitconfig snapshot"))?;
    durable(&sidecar(run_dir), &bytes)?;
    Ok(true)
}

fn sidecar(run_dir: &Path) -> PathBuf {
    run_dir.join("gitconfig.snapshot")
}

fn safe(path: &Path) -> io::Result<()> {
    for ancestor in path.ancestors() {
        match fs::symlink_metadata(ancestor) {
            Ok(m) if m.file_type().is_symlink() => {
                return Err(io::Error::other("gitconfig_untrusted_gitdir"));
            }
            Err(e) if e.kind() != io::ErrorKind::NotFound => return Err(e),
            _ => {}
        }
    }
    Ok(())
}

fn durable(path: &Path, bytes: &[u8]) -> io::Result<()> {
    atomic_write_private(path, bytes)?;
    fs::File::open(path)?.sync_all()?;
    #[cfg(unix)]
    fs::File::open(path.parent().unwrap())?.sync_all()?;
    Ok(())
}

pub(crate) fn run_dir(state: &Path, id: &str) -> io::Result<PathBuf> {
    pg_worker_core::run::record::run_dir(state, id).map_err(|_| io::Error::other("invalid run id"))
}

pub(crate) fn enforce(
    record: &mut RunRecord,
    outcome: Status,
    diagnostic: serde_json::Value,
) -> Status {
    if record.get("gitconfig_guard") != Some(&json!(true)) {
        return outcome;
    }
    let parent = record.path().parent();
    let dir = parent
        .and_then(Path::parent)
        .and_then(Path::parent)
        .zip(record.get("run_id"))
        .and_then(|(state, id)| run_dir(state, id.as_str()?).ok());
    let layout_error = dir.as_deref() != parent || dir.is_none();
    let snapshots = (!layout_error)
        .then(|| dir.as_deref().and_then(|dir| fs::read(sidecar(dir)).ok()))
        .flatten()
        .and_then(|b| serde_json::from_slice::<Vec<Snapshot>>(&b).ok());
    let paths: Vec<_> = snapshots
        .as_ref()
        .map(|s| {
            s.iter()
                .filter(|s| {
                    safe(&s.path).is_err() || optional(&s.path).ok().as_ref() != Some(&s.bytes)
                })
                .map(|s| s.path.clone())
                .collect()
        })
        .unwrap_or_default();
    let previous = record.get("gitconfig_original_outcome").is_some();
    if previous && snapshots.is_none() && !layout_error {
        return Status::Failed;
    }
    if snapshots.is_some() && paths.is_empty() {
        return if previous { Status::Failed } else { outcome };
    }
    let reason = if layout_error {
        "gitconfig_guard_error"
    } else if snapshots.is_none() {
        "gitconfig_snapshot_missing"
    } else {
        "gitconfig_tampered"
    };
    // Keep the original result, including its diagnostic, separate from the
    // security outcome. Finalize's subsequent terminal transition is a no-op.
    if !previous {
        record.set(
            "gitconfig_original_outcome",
            json!({"status":outcome.as_str(),
            "reason":diagnostic["reason"],
            "exit_code":diagnostic["exit_code"]}),
        );
    }
    record.set("reason", json!(reason));
    record.set("gitconfig_changed_paths", json!(paths));
    record.set("status", json!("failed"));
    record.set("exit_code", json!(1));
    record.set("ended", json!(pg_worker_core::run::finalize::now()));
    if record
        .push_event(json!({"event":reason, "paths":paths}))
        .is_err()
    {
        record.set("gitconfig_delivery_error", json!(true));
    }
    let persisted = save_verdict(record);
    let error = snapshots.map(|s| restore(&s)).unwrap_or(true);
    record.set("gitconfig_restore_error", json!(error));
    if !persisted || !save_verdict(record) {
        record.set("gitconfig_verdict_save_error", json!(true));
        if !save_verdict(record) && !layout_error {
            let _ = durable(
                &dir.unwrap().join("gitconfig.verdict-save-error"),
                b"gitconfig_verdict_save_error\n",
            );
        }
    }
    Status::Failed
}

fn save_verdict(record: &RunRecord) -> bool {
    (0..2).any(|_| record.save().is_ok() && durable_record(record).is_ok())
}

fn durable_record(record: &RunRecord) -> io::Result<()> {
    fs::File::open(record.path())?.sync_all()?;
    #[cfg(unix)]
    fs::File::open(record.path().parent().unwrap())?.sync_all()?;
    Ok(())
}

fn restore(snapshots: &[Snapshot]) -> bool {
    let mut failed = false;
    for snapshot in snapshots {
        for _ in 0..2 {
            if safe(snapshot.path.parent().unwrap()).is_err() {
                failed = true;
                break;
            }
            if fs::symlink_metadata(&snapshot.path).is_ok_and(|m| m.file_type().is_symlink())
                && fs::remove_file(&snapshot.path).is_err()
            {
                failed = true;
                break;
            }
            if optional(&snapshot.path).ok().as_ref() == Some(&snapshot.bytes) {
                break;
            }
            let result = match &snapshot.bytes {
                Some(bytes) => atomic_write_private(&snapshot.path, bytes),
                None => match fs::remove_file(&snapshot.path) {
                    Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
                    result => result,
                },
            };
            failed |= result.is_err();
        }
        failed |= safe(&snapshot.path).is_err()
            || optional(&snapshot.path).ok().as_ref() != Some(&snapshot.bytes);
    }
    failed
}

#[cfg(test)]
#[path = "gitconfig_guard_tests.rs"]
mod tests;
