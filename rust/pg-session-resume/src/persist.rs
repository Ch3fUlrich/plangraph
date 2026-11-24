//! What a stop leaves on disk besides the launch record: the lane copy and the sidecar of a
//! stop that is not finished (SESSION-AUTO-CLOSE, docs/SESSION-LIFECYCLE.md).

use crate::engine::Config;
use crate::host::Host;
use crate::record::{load_all, write_private, Record};
use serde_json::{json, Value};
use std::path::PathBuf;

/// One order for the copies of a lane: the later stop wins, a stop of the same second is told
/// apart by its launch, and the session name settles what is left.
fn stop_order(stopped_ts: Option<u64>, launch_ts: Option<u64>, name: &str) -> (u64, u64, String) {
    (
        stopped_ts.unwrap_or(0),
        launch_ts.unwrap_or(0),
        name.to_owned(),
    )
}

fn record_order(rec: &Record) -> (u64, u64, String) {
    stop_order(
        rec.u64("stopped_ts"),
        rec.resume_u64("launch_ts"),
        &rec.label(),
    )
}

/// The lane's resume file: the lane-side copy of the launch record's resume handle. A copy of
/// a later stop is never replaced by an earlier one (publication and repair both come here).
/// Returns false when it could not be written.
pub fn write_lane_resume(host: &mut dyn Host, cfg: &Config, rec: &Record, lane: &str) -> bool {
    let path = cfg.pr_dir.join(format!("{lane}.resume.json"));
    let on_disk: Option<Value> = std::fs::read(&path)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok());
    let theirs = on_disk.as_ref().map(|v| {
        stop_order(
            v.get("stopped_ts").and_then(Value::as_u64),
            v.get("launch_ts").and_then(Value::as_u64),
            v.get("session_name")
                .and_then(Value::as_str)
                .unwrap_or_default(),
        )
    });
    if theirs.is_some_and(|t| t >= record_order(rec)) {
        return true;
    }
    let body = json!({"lane": lane, "state": "stopped", "session_name": rec.label(), "agent": rec.agent().name(),
        "handle": rec.resume_str("handle"), "head": rec.resume_str("head"), "round": rec.resume_round(),
        "stopped_ts": rec.u64("stopped_ts"), "launch_ts": rec.resume_u64("launch_ts"),
        "ts": host.now()});
    let done = write_private(&path, body.to_string().as_bytes());
    if let Err(e) = &done {
        host.log(&format!(
            "event=lane-record-failed name={} lane={lane} error={e}",
            rec.label()
        ));
    }
    done.is_ok()
}

/// Every lane gets the copy of its latest stopped launch; the stop itself is never repeated.
pub fn repair_lane_copies(host: &mut dyn Host, cfg: &Config) -> u32 {
    let mut latest: Vec<(String, Record)> = vec![];
    for rec in load_all(&cfg.launch_dir) {
        let Some(lane) = rec
            .lane()
            .filter(|_| rec.stopped() && rec.resume().is_some())
        else {
            continue;
        };
        match latest.iter_mut().find(|(l, _)| *l == lane) {
            Some((_, best)) if record_order(best) >= record_order(&rec) => {}
            Some(entry) => entry.1 = rec,
            None => latest.push((lane, rec)),
        }
    }
    let mut failed = 0;
    for (lane, rec) in latest {
        if !write_lane_resume(host, cfg, &rec, &lane) {
            failed += 1;
        }
    }
    failed
}

/// What a stop will publish, taken before anything is signalled.
pub fn snapshot(
    host: &dyn Host,
    rec: &Record,
    handle: &str,
    reason: &str,
    status_head: Option<&str>,
) -> Value {
    let cwd = rec.str("cwd").map(PathBuf::from);
    // A failed lookup stays unknown, never a substitute.
    json!({"agent": rec.agent().name(), "handle": handle, "reason": reason,
        "head": cwd.as_deref().and_then(|c| host.git_head(c)),
        "worktree": cwd.as_deref().and_then(|c| host.worktree_id(c)),
        "status_head": status_head, "round": rec.resume_round(), "stopped_ts": host.now(),
        "launch_ts": rec.u64("ts"), "launch_run": rec.str("run_id")})
}

/// How far a stop got, written into its sidecar. Only `STOPPED` is evidence that the session
/// was stopped by this stop: an `ATTEMPT` may have failed, been aborted or crashed.
pub const ATTEMPT: &str = "attempt";
/// The stop command succeeded; the record and the log line may still be missing.
pub const STOPPED: &str = "stopped";
/// The stop line is in the log; only the removal of the sidecar is left.
pub const LOGGED: &str = "logged";

/// The sidecar of a stop that is not finished: `<record>.pending`, the snapshot taken before the
/// session was touched, with its phase. It exists from before the signal until the stop line is
/// in the log, so a failed record write or log append is finished by a later tick from the same
/// snapshot, and never from a second look at a worktree that moved on. Not a `.json`: it is no
/// launch record.
pub fn pending_path(rec: &Record) -> PathBuf {
    rec.path.with_extension("pending")
}

/// The phase a sidecar names. No `phase` key is an attempt (a sidecar nobody finished writing);
/// any other value is `Unknown`: it proves nothing and is never acted on as a stop.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Phase {
    Attempt,
    Stopped,
    Logged,
    Unknown,
}

pub fn phase(snap: &Value) -> Phase {
    match snap.get("phase") {
        None => Phase::Attempt,
        Some(Value::String(s)) if s == ATTEMPT => Phase::Attempt,
        Some(Value::String(s)) if s == STOPPED => Phase::Stopped,
        Some(Value::String(s)) if s == LOGGED => Phase::Logged,
        Some(_) => Phase::Unknown,
    }
}

fn same_launch(snap: &Value, rec: &Record) -> bool {
    snap.get("handle").and_then(Value::as_str) == rec.handle()
        && snap.get("launch_ts").and_then(Value::as_u64) == rec.u64("ts")
        && snap.get("launch_run").and_then(Value::as_str) == rec.str("run_id")
}

/// The sidecar's snapshot when it belongs to this launch.
pub fn read_pending(rec: &Record) -> Option<Value> {
    let snap: Value = serde_json::from_slice(&std::fs::read(pending_path(rec)).ok()?).ok()?;
    same_launch(&snap, rec).then_some(snap)
}

/// Write the sidecar with `phase` (owner-only, atomic); the snapshot's own phase moves with it.
pub fn write_pending(rec: &Record, snap: &mut Value, phase: &str) -> std::io::Result<()> {
    snap["phase"] = json!(phase);
    write_private(&pending_path(rec), snap.to_string().as_bytes())
}

/// Remove the sidecar; one that stays behind is reported (the run exits non-zero).
pub fn drop_pending(host: &mut dyn Host, rec: &Record, failures: &mut u32) {
    match std::fs::remove_file(pending_path(rec)) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => {
            *failures += 1;
            host.log(&format!(
                "event=stop-pending-left name={} error={e}",
                rec.label()
            ));
        }
    }
}
