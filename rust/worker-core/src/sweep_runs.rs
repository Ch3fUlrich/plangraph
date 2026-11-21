//! Run ownership and terminal status authority for native and legacy worktrees.
use serde_json::Value;
use std::{
    fs, io,
    path::{Path, PathBuf},
};

pub use crate::run::record::releases_ownership as terminal;

/// Preserve the original global refusal for unrecognized protocol statuses.
pub fn known(status: &str) -> bool {
    terminal(status)
        || crate::run::record::Status::parse(status).is_some()
        || matches!(status, "starting" | "claimed" | "unknown" | "cancelling")
}

fn finished(row: &Value) -> io::Result<bool> {
    let status = row["status"]
        .as_str()
        .ok_or_else(|| io::Error::other("run status unavailable"))?;
    if !known(status) {
        return Err(io::Error::other("unknown run status"));
    }
    let survivors = match row.get("survivors") {
        None => false,
        Some(Value::Array(pids)) => !pids.is_empty(),
        Some(_) => return Err(io::Error::other("invalid survivor records")),
    };
    Ok(terminal(status) && !survivors)
}
fn read(path: &Path) -> io::Result<Value> {
    serde_json::from_slice(&fs::read(path)?).map_err(io::Error::other)
}
fn root(state: &Path, name: &str) -> io::Result<PathBuf> {
    let path = state.join(name);
    match path.canonicalize() {
        Ok(path) => Ok(path),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(path),
        Err(error) => Err(error),
    }
}
fn paths(row: &Value) -> io::Result<Vec<PathBuf>> {
    let mut values = vec![row.get("checkout"), row.get("cwd")];
    if let Some(worktree) = row.get("run_worktree").filter(|v| !v.is_null()) {
        values.push(Some(&worktree["path"]));
    }
    if let Some(attempts) = row.get("worktree_attempts").filter(|v| !v.is_null()) {
        for attempt in attempts
            .as_array()
            .ok_or_else(|| io::Error::other("invalid worktree attempts"))?
        {
            values.push(Some(&attempt["path"]));
        }
    }
    let mut paths = Vec::new();
    for value in values.into_iter().flatten().filter(|v| !v.is_null()) {
        let value = if value.is_object() {
            &value["path"]
        } else {
            value
        };
        let path = PathBuf::from(
            value
                .as_str()
                .ok_or_else(|| io::Error::other("invalid run checkout"))?,
        );
        if !path.is_absolute() {
            return Err(io::Error::other("run checkout must be absolute"));
        }
        paths.push(path.canonicalize().unwrap_or(path));
    }
    Ok(paths)
}
/// Native paths have a direct owning record; legacy attempt paths need explicit metadata.
pub fn check(state: &Path, path: &Path) -> io::Result<()> {
    if state.as_os_str().is_empty() {
        return Err(io::Error::other("empty run state path"));
    }
    let state = state.canonicalize()?;
    let path = path.canonicalize()?;
    // Inventory records resolved worktree paths, including relocated state roots.
    let native = root(&state, "runs")?;
    let legacy = root(&state, "worktrees")?;
    if let Ok(relative) = path.strip_prefix(&native) {
        let run = relative
            .components()
            .next()
            .ok_or_else(|| io::Error::other("run path unavailable"))?;
        if !finished(&read(&native.join(run).join("record.json"))?)? {
            return Err(io::Error::other("nonterminal run"));
        }
    } else if path.starts_with(legacy) {
        let mut owned = false;
        for entry in fs::read_dir(&native)? {
            let row = read(&entry?.path().join("record.json"))?;
            let done = finished(&row)?;
            // A selected clone ancestor does not prove ownership of an attempt.
            if paths(&row)?.iter().any(|p| p.starts_with(&path)) {
                owned = true;
                if !done {
                    return Err(io::Error::other("nonterminal run"));
                }
            }
        }
        if !owned {
            return Err(io::Error::other("legacy run record unavailable"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn stalled_no_leg_lets_native_and_legacy_worktrees_be_swept() {
        let state = tempfile::tempdir().unwrap();
        let run = state.path().join("runs/run-fixture");
        let native = run.join("checkout");
        let legacy = state.path().join("worktrees/attempt");
        fs::create_dir_all(&native).unwrap();
        fs::create_dir_all(&legacy).unwrap();
        let mut record = json!({"status":"stalled_no_leg","cwd":legacy});
        let record_path = run.join("record.json");
        fs::write(&record_path, record.to_string()).unwrap();
        check(state.path(), &native).unwrap();
        check(state.path(), &legacy).unwrap();

        record["survivors"] = json!([123]);
        fs::write(record_path, record.to_string()).unwrap();
        assert!(check(state.path(), &native).is_err());
        assert!(check(state.path(), &legacy).is_err());
    }
}
