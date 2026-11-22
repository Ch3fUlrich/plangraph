//! Owner-local, cooperative stop of a native writer at a quiescent boundary.
use pg_worker_core::{lockfile, run::record_io::atomic_write_private};
use serde_json::{json, Value};
use std::{fs, path::Path, process::Command};

pub(crate) const STOPPED: &str = "writer stopped after landing";

fn directory(record: &Path) -> Result<&Path, String> {
    record
        .parent()
        .ok_or_else(|| "writer record needs a run directory".into())
}

fn marker(record: &Path) -> Result<std::path::PathBuf, String> {
    Ok(directory(record)?.join("landing-stop.json"))
}

fn lock(record: &Path) -> Result<Option<lockfile::LockGuard>, String> {
    lockfile::try_lock(&directory(record)?.join("writer-control.lock")).map_err(|e| e.to_string())
}

pub(crate) fn supervisor_guard(record: &Path) -> Result<lockfile::LockGuard, String> {
    lockfile::try_lock(&directory(record)?.join("writer-supervisor.lock"))
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "writer supervisor is still active".into())
}

pub(crate) fn mutation_guard(record: &Path) -> Result<lockfile::LockGuard, String> {
    let guard = lock(record)?.ok_or("writer control is checking the worktree")?;
    if marker(record)?.exists() {
        return Err(STOPPED.into());
    }
    Ok(guard)
}

fn read(path: &Path) -> Result<Value, String> {
    let bytes = fs::read(path).map_err(|_| "writer record unavailable")?;
    serde_json::from_slice(&bytes).map_err(|_| "invalid writer record".into())
}

fn git(root: &Path, args: &[&str]) -> Result<String, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .map_err(|_| "cannot run git for writer comparison")?;
    if !output.status.success() {
        return Err(format!("git {} could not prove writer state", args[0]));
    }
    String::from_utf8(output.stdout)
        .map(|s| s.trim_end().into())
        .map_err(|_| "invalid writer Git output".into())
}

fn sha(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn hosted(root: &Path, branch: &str) -> Result<(String, String), String> {
    let refs = git(
        root,
        &["ls-remote", "--refs", "origin", "refs/heads/main", branch],
    )?;
    let find = |wanted| {
        refs.lines()
            .filter_map(|line| line.split_once('\t'))
            .find(|(head, name)| *name == wanted && sha(head))
            .map(|(head, _)| head.to_owned())
            .ok_or("hosted writer branch or main is unavailable")
    };
    Ok((find("refs/heads/main")?, find(branch)?))
}

fn proof(record: &Path, run: &Value, landed: &str) -> Result<Value, String> {
    if run["run_kind"] != "native-codex" || run["overflow_remote"] == true {
        return Err("stop must run on the native writer's owning host".into());
    }
    let session = run["session_id"]
        .as_str()
        .filter(|id| !id.is_empty())
        .ok_or("writer has no resumable session")?;
    let cwd = Path::new(run["cwd"].as_str().ok_or("writer worktree unavailable")?);
    let expected = directory(record)?.join("worktree");
    if cwd
        .canonicalize()
        .ok()
        .zip(expected.canonicalize().ok())
        .is_none_or(|(a, b)| a != b)
    {
        return Err("writer record worktree differs from its owner-local worktree".into());
    }
    let request = read(&directory(record)?.join("native-request.json")).ok();
    let branch = run["pushed"]
        .as_str()
        .or_else(|| request.as_ref()?.get("card")?.get("push_ref")?.as_str())
        .filter(|r| r.starts_with("refs/heads/") && *r != "refs/heads/main")
        .ok_or("writer has no hosted branch")?;
    git(cwd, &["check-ref-format", branch])?;
    let clean = || {
        git(cwd, &["status", "--porcelain=v1", "--untracked-files=all"]).and_then(|s| {
            if s.is_empty() {
                Ok(())
            } else {
                Err("writer has uncommitted changes".into())
            }
        })
    };
    clean()?;
    let head = git(cwd, &["rev-parse", "HEAD"])?;
    let (main, remote) = hosted(cwd, branch)?;
    if remote != head {
        return Err("writer has unpushed work or its hosted branch moved".into());
    }
    if git(cwd, &["cat-file", "-e", &format!("{main}^{{commit}}")]).is_err() {
        git(
            cwd,
            &["fetch", "-q", "--no-tags", "origin", "refs/heads/main"],
        )?;
    }
    git(cwd, &["merge-base", "--is-ancestor", landed, &main])
        .map_err(|_| "landing head is not in hosted main")?;
    git(cwd, &["merge-base", "--is-ancestor", &head, landed])
        .map_err(|_| "writer branch is not contained in the landing")?;
    clean()?;
    if git(cwd, &["rev-parse", "HEAD"])? != head || hosted(cwd, branch)? != (main, remote) {
        return Err("writer or hosted main changed during landing verification".into());
    }
    Ok(
        json!({"status":"stopped", "run_id":run["run_id"], "session_id":session,
              "head":head, "landed":landed}),
    )
}

fn stop(record: &Path, landed: &str) -> Result<Value, String> {
    let _guard = lock(record)?.ok_or("writer has an active worktree operation")?;
    let path = marker(record)?;
    if path.is_file() {
        return read(&path);
    }
    let run = read(record)?;
    let receipt = proof(record, &run, landed)?;
    atomic_write_private(&path, receipt.to_string().as_bytes())
        .map_err(|_| "cannot preserve writer stop receipt")?;
    Ok(receipt)
}

pub(crate) fn stop_cli(args: Vec<String>) -> Result<(), String> {
    let [head, paths @ ..] = args.as_slice() else {
        return Err("usage: pg-spawnd writer-stop MERGED_SHA RECORD...".into());
    };
    if !sha(head) || paths.is_empty() {
        return Err("writer-stop requires a full merged SHA and at least one record".into());
    }
    let rows: Vec<_> = paths
        .iter()
        .map(|path| {
            stop(Path::new(path), head)
                .unwrap_or_else(|reason| json!({"status":"kept", "record":path, "reason":reason}))
        })
        .collect();
    println!("{}", json!(rows));
    Ok(())
}

/// Retire the cooperative stop only after its supervisor has reached a terminal boundary.
pub(crate) fn take_resume(record: &Path) -> Result<Value, String> {
    let _guard = lock(record)?.ok_or("writer still has an active worktree operation")?;
    let run = read(record)?;
    let path = marker(record)?;
    if run["landing_resume"] == true && !path.exists() {
        return Ok(run);
    }
    if !matches!(
        run["status"].as_str(),
        Some("stopped" | "completed" | "failed")
    ) {
        return Err("writer supervisor has not finished stopping".into());
    }
    let receipt = read(&path)?;
    if receipt["status"] != "stopped"
        || receipt["session_id"] != run["session_id"]
        || receipt["run_id"] != run["run_id"]
    {
        return Err("stored session differs from the stopped writer".into());
    }
    let mut resumed = run;
    resumed["landing_resume"] = json!(true);
    // Persist the resume request before releasing the old stop marker so a
    // caller can replay it if the owner dies at this boundary.
    atomic_write_private(record, resumed.to_string().as_bytes())
        .map_err(|_| "cannot persist writer resume")?;
    fs::remove_file(path).map_err(|_| "cannot release writer stop")?;
    Ok(resumed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resume_reuses_the_preserved_session_after_an_interrupted_handoff() {
        let tmp = tempfile::tempdir().unwrap();
        let record = tmp.path().join("record.json");
        let marker = marker(&record).unwrap();
        let row =
            json!({"run_id":"run-fixture", "session_id":"fixture-session", "status":"stopped"});
        fs::write(&record, row.to_string()).unwrap();
        fs::write(&marker, row.to_string()).unwrap();
        let _supervisor = supervisor_guard(&record).unwrap();
        assert!(supervisor_guard(&record).is_err());
        assert!(mutation_guard(&record).is_err());
        let resume = take_resume(&record).unwrap();
        assert_eq!(resume["session_id"], "fixture-session");
        assert!(!marker.exists());
        assert_eq!(take_resume(&record).unwrap(), resume);
    }
}
