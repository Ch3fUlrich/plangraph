//! Durable run inventory, row locking, and conservative process reconciliation.
use crate::reconcile::ReconcileError;
use pg_worker_core::run::record::valid_run_id;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{io::Read, path::Path};
#[derive(Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Proc {
    pub pid: u32,
    pub start_time: Option<u64>,
    pub group: Option<String>,
    pub pgid: Option<u32>,
}
pub(crate) struct Entry {
    pub run_id: String,
    pub role: String,
    pub attempt: u32,
    pub identity: Option<Proc>,
    pub deadline_ms: Option<u64>,
}
#[path = "reconcile_store_helpers.rs"]
mod helpers;
pub(crate) use helpers::lock;
use helpers::{check, seconds};
pub(crate) fn read(path: &Path) -> Result<Value, ReconcileError> {
    check(path, false)?;
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options.open(path).map_err(|_| ReconcileError::Io)?;
    let meta = file.metadata().map_err(|_| ReconcileError::Io)?;
    const CAP: u64 = 8 * 1024 * 1024;
    if !meta.is_file() || meta.len() > CAP {
        return Err(ReconcileError::Invalid);
    }
    let mut bytes = Vec::new();
    file.take(CAP + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| ReconcileError::Io)?;
    if bytes.len() as u64 > CAP {
        return Err(ReconcileError::Invalid);
    }
    serde_json::from_slice(&bytes).map_err(|_| ReconcileError::Invalid)
}
pub(crate) fn inventory(root: &Path) -> Result<Vec<Entry>, ReconcileError> {
    match std::fs::symlink_metadata(root) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        _ => check(root, true)?,
    }
    let runs = root.join("runs");
    match std::fs::symlink_metadata(&runs) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        _ => check(&runs, true)?,
    }
    let mut paths = std::fs::read_dir(runs)
        .map_err(|_| ReconcileError::Io)?
        .map(|e| e.map(|e| e.path()).map_err(|_| ReconcileError::Io))
        .collect::<Result<Vec<_>, _>>()?;
    paths.sort();
    match std::fs::symlink_metadata(root.join("reconcile.lock")) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
        _ => check(&root.join("reconcile.lock"), false)?,
    }
    let mut result = Vec::new();
    for path in paths {
        let mut rows = Vec::new();
        let parsed = (|| -> Result<(), ReconcileError> {
            if !path.join("record.json").is_file() {
                return Ok(());
            }
            let id = path
                .file_name()
                .and_then(|s| s.to_str())
                .filter(|s| valid_run_id(s))
                .ok_or(ReconcileError::Invalid)?;
            check(&path, true)?;
            {
                let lock = path.join("record.lock");
                match std::fs::symlink_metadata(&lock) {
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
                    _ => check(&lock, false)?,
                }
            }
            let record = read(&path.join("record.json"))?;
            if record["run_id"].as_str() != Some(id) {
                return Err(ReconcileError::Invalid);
            }
            if record["status"] != "running" || !record["ended"].is_null() {
                return Ok(());
            }
            let role = record["role"].as_str().ok_or(ReconcileError::Invalid)?;
            crate::reconcile::mode(None, &Value::Null, role)?;
            let mut attempts = record["attempts"]
                .as_array()
                .ok_or(ReconcileError::Invalid)?
                .clone();
            if attempts.is_empty() {
                attempts.push(json!({"status":"running", "proc":record["proc"],
                "started":record["started"], "leg":record["plan"]["first_leg"]}));
            }
            for (index, attempt) in attempts.iter().enumerate() {
                if attempt["status"] != "running" {
                    continue;
                }
                if !attempt["leg"].is_object() {
                    return Err(ReconcileError::Invalid);
                }
                let started = seconds(attempt.get("started").unwrap_or(&record["started"]))?;
                let max_s = attempt
                    .get("max_s")
                    .or_else(|| record.get("max_s"))
                    .filter(|v| !v.is_null())
                    .map(|v| v.as_u64().ok_or(ReconcileError::Invalid))
                    .transpose()?;
                // In-flight attempts use the run-level launch identity.
                let identity_value = attempt
                    .get("proc")
                    .filter(|p| !p.is_null())
                    .or_else(|| (index + 1 == attempts.len()).then_some(&record["proc"]))
                    .unwrap_or(&Value::Null);
                let identity = if identity_value.is_null() {
                    None
                } else {
                    let proc: Proc = serde_json::from_value(identity_value.clone())
                        .map_err(|_| ReconcileError::Invalid)?;
                    if proc.pid == 0
                        || proc.start_time == Some(0)
                        || proc.pgid.is_some_and(|id| id == 0 || id > i32::MAX as u32)
                        || proc.group.as_ref().is_some_and(|g| g.is_empty())
                    {
                        return Err(ReconcileError::Invalid);
                    }
                    Some(proc)
                };
                rows.push(Entry {
                    run_id: id.into(),
                    role: role.into(),
                    attempt: u32::try_from(index + 1).map_err(|_| ReconcileError::Invalid)?,
                    identity,
                    deadline_ms: crate::reconcile_time::deadline(started, max_s),
                });
            }
            Ok(())
        })();
        if parsed.is_ok() {
            result.extend(rows);
        }
    }
    Ok(result)
}
/// Settlement requires both the recorded process and its confinement group gone.
pub(crate) fn gone(run_id: &str, identity: Option<&Proc>) -> Result<bool, ReconcileError> {
    let Some(identity) = identity else {
        return Err(ReconcileError::Unknown);
    };
    #[cfg(target_os = "linux")]
    {
        if let Some(group) = &identity.group {
            let path = crate::reconcile_backend::native::scope(run_id, group)
                .map_err(|_| ReconcileError::Unknown)?;
            return match std::fs::read_to_string(path.join("cgroup.events")) {
                Ok(text) => match text.lines().find_map(|s| s.strip_prefix("populated ")) {
                    Some("0") => Ok(true),
                    Some("1") => crate::reconcile::populated_alive(identity, &path),
                    _ => Err(ReconcileError::Unknown),
                },
                Err(e)
                    if e.kind() == std::io::ErrorKind::NotFound
                        && !path.try_exists().map_err(|_| ReconcileError::Unknown)? =>
                {
                    Ok(true)
                }
                Err(_) => Err(ReconcileError::Unknown),
            };
        }
        match std::fs::read_to_string(format!("/proc/{}/stat", identity.pid)) {
            Ok(stat) => {
                let start = stat
                    .rsplit_once(')')
                    .and_then(|(_, rest)| rest.split_whitespace().nth(19)?.parse::<u64>().ok())
                    .ok_or(ReconcileError::Unknown)?;
                if !identity
                    .start_time
                    .is_some_and(|expected| expected != start)
                {
                    return Ok(false);
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(_) => return Err(ReconcileError::Unknown),
        }
        let pgid = identity.pgid.ok_or(ReconcileError::Unknown)?;
        let pgid = i32::try_from(pgid)
            .ok()
            .filter(|id| *id > 0)
            .ok_or(ReconcileError::Unknown)?;
        // SAFETY: pgid is a checked positive i32; signal zero only probes the process group.
        if unsafe { libc::kill(-pgid, 0) } == 0 {
            return Ok(false);
        }
        match std::io::Error::last_os_error().raw_os_error() {
            Some(libc::ESRCH) => Ok(true),
            Some(libc::EPERM) => Ok(false),
            _ => Err(ReconcileError::Unknown),
        }
    }
    #[cfg(windows)]
    {
        let name = identity.group.as_deref().ok_or(ReconcileError::Unknown)?;
        crate::reconcile::job::scope(run_id, name).map_err(|_| ReconcileError::Unknown)?;
        let copy = Proc {
            pid: identity.pid,
            start_time: identity.start_time,
            group: identity.group.clone(),
            pgid: identity.pgid,
        };
        match crate::reconcile::job::Group::acquire(run_id, copy)
            .map_err(|_| ReconcileError::Unknown)?
        {
            Some(mut job) => crate::reconcile::job::map_probe(job.probe()),
            None => crate::reconcile::job::absent(identity.pid),
        }
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = (run_id, identity);
        Err(ReconcileError::Unknown)
    }
}
/// Caller holds the state lock; re-read under the record lock before proving gone.
pub(crate) fn orphan_gone(root: &Path, row: &Entry) -> Result<bool, ReconcileError> {
    orphan_with(root, row, &mut None)
}
pub(crate) fn orphan_with(
    root: &Path,
    row: &Entry,
    #[cfg(windows)] authority: &mut Option<Option<crate::reconcile::job::Group>>,
    #[cfg(not(windows))] _authority: &mut Option<()>,
) -> Result<bool, ReconcileError> {
    use pg_worker_core::run::record::{RunRecord, Status};
    let dir = root.join("runs").join(&row.run_id);
    let _lock = lock(&dir, &dir.join("record.lock"))?;
    let current = inventory(root)?
        .into_iter()
        .find(|entry| entry.run_id == row.run_id && entry.attempt == row.attempt);
    if current
        .as_ref()
        .is_some_and(|current| current.identity != row.identity || current.role != row.role)
    {
        return Err(ReconcileError::Unknown);
    }
    #[cfg(windows)]
    let result = crate::reconcile::job::settle_probe(row, authority);
    #[cfg(not(windows))]
    let result = gone(&row.run_id, row.identity.as_ref());
    if !result? {
        return Ok(false);
    }
    if current.is_none()
        && !dir
            .join("record.json")
            .try_exists()
            .map_err(|_| ReconcileError::Unknown)?
    {
        return Ok(true);
    }
    let mut record = RunRecord::read(root, &row.run_id).map_err(|_| ReconcileError::Io)?;
    if record.status() != Some(Status::Running) {
        return helpers::terminal(record.status());
    }
    if current.is_none() {
        return Ok(true);
    }
    if record
        .get("attempts")
        .and_then(Value::as_array)
        .is_some_and(Vec::is_empty)
    {
        record.set(
            "attempts",
            json!([{"status":"running", "started":record.get("started"),
            "leg":record.get("plan").and_then(|plan| plan.get("first_leg"))}]),
        );
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| ReconcileError::Invalid)?
        .as_millis();
    settle(
        root,
        &mut record,
        row.attempt,
        "gone",
        u64::try_from(now).map_err(|_| ReconcileError::Invalid)?,
    )?;
    Ok(true)
}
/// Durable record-only settlement bridge, under state and record locks.
pub(crate) fn settle(
    state: &Path,
    record: &mut pg_worker_core::run::record::RunRecord,
    attempt: u32,
    how: &str,
    now_ms: u64,
) -> Result<(), ReconcileError> {
    use pg_worker_core::run::record::Status;
    let invalid = || ReconcileError::Invalid;
    let allowed = [
        "gone",
        "ended_unobserved",
        "killed_max_s",
        "reattach_timeout",
    ];
    if !allowed.contains(&how) {
        return Err(invalid());
    }
    let run = record
        .get("run_id")
        .and_then(Value::as_str)
        .filter(|s| valid_run_id(s))
        .ok_or_else(invalid)?
        .to_owned();
    if record.path() != state.join("runs").join(&run).join("record.json") {
        return Err(invalid());
    }
    let mut attempts = record
        .get("attempts")
        .and_then(Value::as_array)
        .ok_or_else(invalid)?
        .clone();
    let index = attempt.checked_sub(1).ok_or_else(invalid)? as usize;
    let entry = attempts
        .get_mut(index)
        .filter(|v| v.is_object())
        .ok_or_else(invalid)?;
    if record.status() == Some(Status::Orphaned)
        && (entry["outcome"]["outcome"] == "orphaned"
            || entry["outcome_skip_reason"].as_str().is_some())
    {
        return Ok(());
    }
    let role = serde_json::from_value(record.get("role").cloned().ok_or_else(invalid)?)
        .map_err(|_| invalid())?;
    let duration = now_ms
        .checked_sub(seconds(
            entry
                .get("started")
                .unwrap_or_else(|| record.get("started").unwrap_or(&Value::Null)),
        )?)
        .ok_or_else(invalid)?
        / 1000;
    match outcome::build(&entry["leg"], role, &run, attempt, now_ms, duration)? {
        Ok(row) => entry["outcome"] = row,
        Err(reason) => {
            let mut log = pg_worker_core::run::livelog::LiveLog::new(
                record.path().with_file_name("live.log"),
                Default::default(),
            );
            log.feed(&format!(
                "reconciliation outcome skipped: run_id={run} attempt={attempt} {reason}\n"
            ));
            log.flush();
            entry["outcome_skip_reason"] = json!(reason);
        }
    }
    entry["how"] = json!(how);
    entry["status"] = json!("orphaned");
    if how == "ended_unobserved" {
        entry["reason"] = json!("reattached_group_exited");
    }
    let diagnostic = entry.clone();
    entry["exit_code"] = Value::Null;
    record
        .transition(Status::Orphaned, now_ms as f64 / 1000.0)
        .map_err(|_| invalid())?;
    record.set("attempts", json!(attempts));
    record
        .push_event(json!({"event":"RUN_ORPHANED", "attempt":attempt,"how":how,
        "idempotency_key":[run,attempt,"RUN_ORPHANED"]}))
        .map_err(|_| invalid())?;
    crate::gitconfig_guard::enforce(record, Status::Orphaned, diagnostic);
    record.save().map_err(|_| ReconcileError::Io)
}
#[path = "reconcile_store_outcome.rs"]
mod outcome;
#[cfg(test)]
#[path = "reconcile_store_tests.rs"]
mod tests;
