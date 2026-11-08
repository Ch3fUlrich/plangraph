//! Time-bounded legacy recovery when no attempt identity can be verified.
use super::*;

pub(crate) fn read(path: &Path) -> Result<Value, ReconcileError> {
    use std::io::Read;
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

// Maximum hold for unknown identities AND Z/X leaders, anchored to the persisted
// deadline, attempt started time, or record mtime; restarting never resets it.
pub(super) const UNKNOWN_IDENTITY_GRACE_MS: u64 = 10_000;

pub(super) fn unverified_not_gone(row: &Entry) -> bool {
    if let Some(identity) = &row.identity {
        if zombie(identity) {
            return !matches!(live_members(identity), Ok(false));
        }
    }
    needs_grace(row) && !matches!(gone(&row.run_id, row.identity.as_ref()), Ok(true))
}

pub(super) fn needs_grace(row: &Entry) -> bool {
    needs_grace_with(row, leader_start)
}

fn needs_grace_with(row: &Entry, probe: impl Fn(u32) -> Option<u64>) -> bool {
    row.identity.as_ref().is_none_or(|p| {
        let Some(expected) = p.start_time else {
            return true;
        };
        probe(if p.pid == 0 {
            p.pgid.unwrap_or(0)
        } else {
            p.pid
        }) != Some(expected)
    })
}

fn leader_start(leader: u32) -> Option<u64> {
    #[cfg(target_os = "linux")]
    {
        // A scope name or a live process group is not a verified leader identity.
        std::fs::read_to_string(format!("/proc/{leader}/stat"))
            .ok()
            .and_then(|stat| {
                let (_, rest) = stat.rsplit_once(')')?;
                let fields: Vec<_> = rest.split_whitespace().collect();
                if matches!(fields.first(), Some(&"Z" | &"X")) {
                    return None;
                }
                fields.get(19)?.parse::<u64>().ok()
            })
    }
    #[cfg(not(target_os = "linux"))]
    {
        // No portable start-time probe here. A scope name is not identity proof;
        // gone() can still prove an empty Windows job before grace is applied.
        let _ = leader;
        None
    }
}

#[cfg(test)]
#[path = "reconcile_store_grace_tests.rs"]
mod tests;

pub(super) fn zombie(identity: &Proc) -> bool {
    #[cfg(target_os = "linux")]
    {
        let pid = if identity.pid == 0 {
            identity.pgid.unwrap_or(0)
        } else {
            identity.pid
        };
        std::fs::read_to_string(format!("/proc/{pid}/stat"))
            .ok()
            .is_some_and(|s| {
                s.rsplit_once(')').is_some_and(|(_, rest)| {
                    matches!(rest.split_whitespace().next(), Some("Z" | "X"))
                })
            })
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = identity;
        false
    }
}

pub(super) fn live_members(identity: &Proc) -> Result<bool, ReconcileError> {
    let pgid = identity.pgid.ok_or(ReconcileError::Unknown)?;
    #[cfg(target_os = "linux")]
    {
        for entry in std::fs::read_dir("/proc").map_err(|_| ReconcileError::Unknown)? {
            let entry = entry.map_err(|_| ReconcileError::Unknown)?;
            if entry.file_name().to_string_lossy().parse::<u32>().is_err() {
                continue;
            }
            let stat = match std::fs::read_to_string(entry.path().join("stat")) {
                Ok(stat) => stat,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Err(_) => return Err(ReconcileError::Unknown),
            };
            let (_, rest) = stat.rsplit_once(')').ok_or(ReconcileError::Unknown)?;
            let fields: Vec<_> = rest.split_whitespace().collect();
            if fields.get(2).and_then(|s| s.parse::<u32>().ok()) == Some(pgid)
                && !matches!(fields.first(), Some(&"Z" | &"X"))
            {
                return Ok(true);
            }
        }
        Ok(false)
    }
    #[cfg(not(target_os = "linux"))]
    {
        // Platform gap: kill(0) cannot distinguish zombies from live members.
        // The persisted grace still bounds the hold on these platforms.
        #[cfg(unix)]
        {
            if unsafe { libc::kill(-(pgid as i32), 0) } == 0 {
                return Ok(true);
            }
            return match std::io::Error::last_os_error().raw_os_error() {
                Some(libc::ESRCH) => Ok(false),
                _ => Err(ReconcileError::Unknown),
            };
        }
        #[cfg(not(unix))]
        {
            let _ = pgid;
            Err(ReconcileError::Unknown)
        }
    }
}

#[cfg(target_os = "linux")]
pub(super) fn pgid_gone(identity: &Proc) -> Result<bool, ReconcileError> {
    let pgid = identity.pgid.ok_or(ReconcileError::Unknown)?;
    if let Some(expected) = identity.start_time {
        match std::fs::read_to_string(format!("/proc/{pgid}/stat")) {
            Ok(stat) => {
                let start = stat
                    .rsplit_once(')')
                    .and_then(|(_, rest)| rest.split_whitespace().nth(19)?.parse::<u64>().ok())
                    .ok_or(ReconcileError::Unknown)?;
                if start != expected {
                    return Ok(true);
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(_) => return Err(ReconcileError::Unknown),
        }
    }
    let pgid = i32::try_from(pgid)
        .ok()
        .filter(|id| *id > 0)
        .ok_or(ReconcileError::Unknown)?;
    if unsafe { libc::kill(-pgid, 0) } == 0 {
        return Ok(false);
    }
    match std::io::Error::last_os_error().raw_os_error() {
        Some(libc::ESRCH) => Ok(true),
        Some(libc::EPERM) => Ok(false),
        _ => Err(ReconcileError::Unknown),
    }
}

pub(super) fn attempts(record: &Value) -> Result<Vec<Value>, ReconcileError> {
    match record.get("attempts") {
        None => Ok(Vec::new()),
        Some(value) => value.as_array().cloned().ok_or(ReconcileError::Invalid),
    }
}

pub(super) fn reclaim(root: &Path, run: &str) -> Result<(), ReconcileError> {
    pg_worker_core::slots::with_table(root, std::time::Duration::from_secs(2), |table| {
        table.active.retain(|slot| slot.run_id != run);
    })
    .map_err(|_| ReconcileError::Io)
}

// Inventory absence is not evidence that the saved confinement has disappeared.
pub(super) fn settle_absent(root: &Path, row: &Entry) -> Result<bool, ReconcileError> {
    use pg_worker_core::run::record::{RunRecord, Status};
    let probe = gone(&row.run_id, row.identity.as_ref());
    if !needs_grace(row) {
        return probe;
    }
    let dir = root.join("runs").join(&row.run_id);
    let path = dir.join("record.json");
    let exists = path.try_exists().map_err(|_| ReconcileError::Unknown)?;
    let record = if exists { Some(read(&path)?) } else { None };
    let started = record.as_ref().and_then(|r| {
        r.get("attempts")
            .and_then(Value::as_array)
            .and_then(|a| a.get(row.attempt as usize - 1))
            .and_then(|a| a.get("started"))
            .or_else(|| r.get("started"))
            .filter(|v| !v.is_null())
    });
    let base = match (row.deadline_ms, started) {
        (Some(deadline), _) => deadline,
        (_, Some(started)) => seconds(started)?,
        _ => std::fs::metadata(if exists { &path } else { &dir })
            .and_then(|m| m.modified())
            .map_err(|_| ReconcileError::Io)?
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| ReconcileError::Invalid)?
            .as_millis() as u64,
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| ReconcileError::Invalid)?
        .as_millis() as u64;
    if now < base.saturating_add(UNKNOWN_IDENTITY_GRACE_MS) {
        return Err(ReconcileError::Unknown);
    }
    if !exists {
        return Ok(true);
    }
    let record = RunRecord::read(root, &row.run_id).map_err(|_| ReconcileError::Io)?;
    if record.status() != Some(Status::Running) {
        return helpers::terminal(record.status());
    }
    settle_unknown(root, row)
}

// Caller holds state and record locks. Restarts never advance the time bound.
pub(super) fn settle_unknown(root: &Path, row: &Entry) -> Result<bool, ReconcileError> {
    use pg_worker_core::run::record::RunRecord;
    let mut record = RunRecord::read(root, &row.run_id).map_err(|_| ReconcileError::Io)?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| ReconcileError::Invalid)?
        .as_millis() as u64;
    let value = read(record.path())?;
    let mut attempts = attempts(&value)?;
    if attempts.is_empty() {
        attempts.push(json!({"status":"running", "started":record.get("started"),
            "leg":record.get("plan").and_then(|p| p.get("first_leg"))}));
    }
    let attempt = attempts
        .get(row.attempt as usize - 1)
        .ok_or(ReconcileError::Invalid)?;
    let base = match row.deadline_ms {
        Some(deadline) => deadline,
        None => match attempt.get("started").filter(|v| !v.is_null()) {
            Some(started) => seconds(started)?,
            None => std::fs::metadata(record.path())
                .and_then(|m| m.modified())
                .map_err(|_| ReconcileError::Io)?
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(|_| ReconcileError::Invalid)?
                .as_millis() as u64,
        },
    };
    if now < base.saturating_add(UNKNOWN_IDENTITY_GRACE_MS) {
        return Err(ReconcileError::Unknown);
    }
    // Accepted legacy risk: an unverifiable identity (including a live pgid
    // without a leader start time) may release capacity while a child lives.
    // Use the same persisted deadline/started/mtime bound across restarts.
    record.set("attempts", json!(attempts));
    settle(root, &mut record, row.attempt, "unknown_identity", now)?;
    Ok(true)
}
