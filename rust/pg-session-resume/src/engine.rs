//! Stop at DONE and reap idle sessions (SESSION-AUTO-CLOSE). Every stop writes one log
//! line; a refusal is logged once per cause.

use crate::agent::claude_stop;
use crate::contract::{glob_match, parse_status, status_path};
use crate::host::{Host, Process};
use crate::record::{load_all, Agent, Record};
use serde_json::{json, Value};
use std::path::PathBuf;

/// Never stopped (case-insensitive globs on name and session_name): live L1s, routing, planners,
/// judges and oversight seats. A superset of the Python reaper's list (`session_reap/policy.py`).
pub const DEFAULT_ALLOW: &[&str] = &[
    "*-L1",
    "*-L1-*",
    "L1",
    "L1-*",
    "routing*",
    "plan-*",
    "overseer*",
    "*planner*",
    "*judge*",
    "plangraph-oversight*",
];

pub struct Config {
    pub launch_dir: PathBuf,
    pub pr_dir: PathBuf,
    pub idle_s: u64,
    /// Globs matched against name and session_name; a match is never stopped.
    pub allow: Vec<String>,
}

fn allowed(cfg: &Config, rec: &Record) -> bool {
    let names = [rec.str("name"), rec.str("session_name")];
    names.iter().flatten().any(|n| {
        let n = n.to_lowercase();
        cfg.allow.iter().any(|g| glob_match(&g.to_lowercase(), &n))
    })
}

fn age(host: &dyn Host, rec: &Record) -> u64 {
    host.now()
        .saturating_sub(rec.u64("ts").unwrap_or_else(|| host.now()))
}

fn one_line(s: &str) -> String {
    s.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(200)
        .collect()
}

/// Save the record; a failure is logged and reported, never silent.
fn save(host: &mut dyn Host, rec: &Record) -> bool {
    match rec.save() {
        Ok(()) => true,
        Err(e) => {
            host.log(&format!(
                "event=record-save-failed name={} error={e}",
                rec.label()
            ));
            false
        }
    }
}

/// Log a refusal once per cause: a record that stays refused must not fill the log every tick.
/// The cause is remembered only after its line is in the log, so a failed append is retried.
fn refuse_once(host: &mut dyn Host, rec: &mut Record, line: &str, why: &str) {
    if rec.str("stop_refused") == Some(why) || !host.log(line) {
        return;
    }
    rec.set("stop_refused", json!(why));
    save(host, rec);
}

/// The lane's resume file: the lane-side copy of the launch record's resume handle.
/// Returns false when it could not be written.
fn write_lane_resume(host: &mut dyn Host, cfg: &Config, rec: &Record, lane: &str) -> bool {
    let body = json!({"lane": lane, "state": "stopped", "session_name": rec.label(), "agent": rec.agent().name(),
        "handle": rec.resume_str("handle"), "head": rec.resume_str("head"), "round": rec.resume_round(),
        "stopped_ts": rec.u64("stopped_ts"), "ts": host.now()});
    let path = cfg.pr_dir.join(format!("{lane}.resume.json"));
    let tmp = path.with_extension("json.tmp");
    let done = std::fs::write(&tmp, body.to_string()).and_then(|_| std::fs::rename(&tmp, &path));
    if let Err(e) = &done {
        host.log(&format!(
            "event=lane-record-failed name={} lane={lane} error={e}",
            rec.label()
        ));
    }
    done.is_ok()
}

/// Every lane gets the copy of its latest stopped launch. A missing copy (its write failed) or
/// one of an older stop is rewritten; the stop itself is never repeated.
fn repair_lane_copies(host: &mut dyn Host, cfg: &Config) -> u32 {
    let mut latest: Vec<(String, Record)> = vec![];
    for rec in load_all(&cfg.launch_dir) {
        let Some(lane) = rec
            .lane()
            .filter(|_| rec.stopped() && rec.resume().is_some())
        else {
            continue;
        };
        match latest.iter_mut().find(|(l, _)| *l == lane) {
            Some((_, best)) if best.u64("stopped_ts") >= rec.u64("stopped_ts") => {}
            Some(entry) => entry.1 = rec,
            None => latest.push((lane, rec)),
        }
    }
    let mut failed = 0;
    for (lane, rec) in latest {
        let path = cfg.pr_dir.join(format!("{lane}.resume.json"));
        let on_disk: Option<Value> = std::fs::read(&path)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok());
        let current = on_disk
            .as_ref()
            .and_then(|v| v.get("stopped_ts")?.as_u64())
            .is_some_and(|t| Some(t) >= rec.u64("stopped_ts"));
        if !current && !write_lane_resume(host, cfg, &rec, &lane) {
            failed += 1;
        }
    }
    failed
}

enum Target {
    /// Run this command to stop the session.
    Stop(Vec<String>),
    /// Signal this process (pid and start time) and wait for it to exit.
    Signal(u32, u64),
    /// The runtime already stopped it; only the record is missing.
    AlreadyStopped,
    /// Leave it running; the string says why.
    Keep(String),
}

/// What stopping this session means right now, from the runtime's own state.
fn stop_target(host: &dyn Host, rec: &Record) -> Target {
    match rec.agent() {
        Agent::Claude => match host.job_state(rec) {
            None => Target::Keep("no-job-state".into()),
            Some(j) if j.state == "stopped" => Target::AlreadyStopped,
            Some(j) if j.state != "done" => Target::Keep(format!("job-{}", j.state)),
            Some(j) => match j.tasks {
                Some(0) => {
                    claude_stop(rec).map_or(Target::Keep("no-stop-target".into()), Target::Stop)
                }
                Some(_) => Target::Keep("job-has-tasks".into()),
                None => Target::Keep("job-tasks-unknown".into()),
            },
        },
        Agent::Codex | Agent::OpenCode => {
            let pid = rec
                .u64("pid")
                .filter(|p| *p > 1)
                .and_then(|p| u32::try_from(p).ok());
            let (Some(pid), Some(start)) = (pid, rec.u64("pid_start")) else {
                return Target::Keep("no-process-identity".into());
            };
            match host.process(pid) {
                // gone, or the pid belongs to another process now: nothing of ours to stop
                Process::Gone => return Target::AlreadyStopped,
                Process::Live(s) if s != start => return Target::AlreadyStopped,
                Process::Live(_) => {}
                Process::Unknown => return Target::Keep("no-runtime-state".into()),
            }
            // A model request in flight leaves no child process: only the runtime's own
            // end-of-turn marker is a positive idle signal.
            match (host.turn_ended(rec), host.has_children(pid)) {
                (Some(true), Some(false)) => Target::Signal(pid, start),
                (None, _) => Target::Keep("no-idle-signal".into()),
                (_, None) => Target::Keep("no-runtime-state".into()),
                (Some(false), _) => Target::Keep("turn-open".into()),
                (_, Some(true)) => Target::Keep("has-children".into()),
            }
        }
    }
}

/// What a stop will publish, taken before anything is signalled and kept in the record as
/// `stop_pending`: a stop whose publication fails is finished from this snapshot, never from a
/// second look at a worktree that may have moved on.
fn snapshot(
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

/// The pending snapshot of this very launch, if the record holds one.
fn pending(rec: &Record, handle: &str) -> Option<Value> {
    let p = rec.value.get("stop_pending").filter(|p| p.is_object())?;
    let same = p.get("handle").and_then(Value::as_str) == Some(handle)
        && p.get("launch_ts").and_then(Value::as_u64) == rec.u64("ts")
        && p.get("launch_run").and_then(Value::as_str) == rec.str("run_id");
    same.then(|| p.clone())
}

/// Publish a carried-out stop from its snapshot: state, stopped_ts and the resume object reach
/// the record in one atomic rename (the launch identity scopes the stop, see Record::stopped),
/// then the lane copy and the log line. A record that cannot be saved is not published.
fn publish_stop(
    host: &mut dyn Host,
    cfg: &Config,
    rec: &mut Record,
    snap: &Value,
    freed: &str,
    failures: &mut u32,
) {
    let name = rec.label();
    let handle = snap["handle"].as_str().unwrap_or_default().to_owned();
    rec.set("state", json!("stopped"));
    rec.set("stopped_ts", snap["stopped_ts"].clone());
    rec.set("stop_refused", Value::Null);
    rec.set("stop_pending", Value::Null);
    rec.set("resume", snap.clone());
    if !save(host, rec) {
        *failures += 1;
        return; // no lane copy and no stop line for a stop the record does not hold
    }
    if let Some(l) = rec.lane() {
        if !write_lane_resume(host, cfg, rec, &l) {
            *failures += 1;
        }
    }
    let age_s = age(host, rec);
    let reason = snap["reason"].as_str().unwrap_or_default();
    host.log(&format!(
        "event=stop name={name} age_s={age_s} reason={reason} ram_freed_mb={freed} handle={handle}"
    ));
}

/// A stop that was signalled but not published (the record could not be saved) is finished as
/// soon as the runtime shows the session stopped, from the snapshot taken before the signal.
fn finish_pending(host: &mut dyn Host, cfg: &Config) -> u32 {
    let mut failures = 0;
    for mut rec in load_all(&cfg.launch_dir) {
        let Some(snap) = rec.handle().and_then(|h| pending(&rec, h)) else {
            continue;
        };
        if !rec.stopped() && matches!(stop_target(host, &rec), Target::AlreadyStopped) {
            publish_stop(host, cfg, &mut rec, &snap, "unknown", &mut failures);
        }
    }
    failures
}

/// Stop one session and record how to resume it. `failures` counts what could not be
/// persisted: the run then exits non-zero.
fn stop_session(
    host: &mut dyn Host,
    cfg: &Config,
    rec: &mut Record,
    reason: &str,
    status_head: Option<&str>,
    failures: &mut u32,
) {
    let name = rec.label();
    let Some(handle) = rec.handle().map(str::to_owned) else {
        let line = format!("event=stop-skipped name={name} reason={reason} why=no-resume-handle");
        refuse_once(host, rec, &line, "no-resume-handle");
        return;
    };
    let before = host.mem_available_mb();
    let target = stop_target(host, rec);
    if let Target::Keep(why) = &target {
        let line = format!("event=stop-skipped name={name} reason={reason} why={why}");
        refuse_once(host, rec, &line, why);
        return;
    }
    let (snap, acted) = match target {
        Target::AlreadyStopped => (
            pending(rec, &handle)
                .unwrap_or_else(|| snapshot(host, rec, &handle, reason, status_head)),
            Ok(()),
        ),
        target => {
            // The snapshot reaches the record before the session is touched.
            let snap = snapshot(host, rec, &handle, reason, status_head);
            rec.set("stop_pending", snap.clone());
            if !save(host, rec) {
                *failures += 1;
                return;
            }
            let acted = match target {
                Target::Stop(argv) => host.run(None, &argv).map(drop),
                Target::Signal(pid, start) => host.terminate(pid, start),
                _ => Ok(()),
            };
            (snap, acted)
        }
    };
    if let Err(e) = acted {
        host.log(&format!(
            "event=stop-failed name={name} reason={reason} error={}",
            one_line(&e)
        ));
        // The session still runs: a snapshot of this moment must not outlive the attempt.
        rec.set("stop_pending", Value::Null);
        save(host, rec);
        return;
    }
    let freed = match (before, host.mem_available_mb()) {
        (Some(b), Some(a)) => a.saturating_sub(b).to_string(),
        _ => "unknown".into(),
    };
    publish_stop(host, cfg, rec, &snap, &freed, failures);
}

/// A writer whose lane status says DONE with a head. `quiet_s`: the session must have shown
/// no activity for that long (0 from the Stop hook). `only`: restrict to the record with this
/// session id (the Stop hook path). Returns the number of records or lane files that could
/// not be written.
pub fn stop_done(host: &mut dyn Host, cfg: &Config, quiet_s: u64, only: Option<&str>) -> u32 {
    let mut failures = finish_pending(host, cfg) + repair_lane_copies(host, cfg);
    for mut rec in load_all(&cfg.launch_dir) {
        if rec.stopped() || only.is_some_and(|id| rec.str("session_id") != Some(id)) {
            continue;
        }
        let Some(lane) = rec.lane() else { continue };
        let Ok(text) = std::fs::read_to_string(status_path(&cfg.pr_dir, &lane)) else {
            continue;
        };
        let facts = parse_status(&lane, &text);
        let Some(head) = facts.head.filter(|_| facts.done) else {
            continue;
        };
        if allowed(cfg, &rec) {
            continue;
        }
        let quiet = host
            .activity_ts(&rec)
            .is_none_or(|t| host.now().saturating_sub(t) >= quiet_s);
        if quiet {
            stop_session(host, cfg, &mut rec, "done", Some(&head), &mut failures);
        }
    }
    failures
}

/// Live sessions idle for `idle_s` outside the allowlist; the count as for `stop_done`.
pub fn reap_idle(host: &mut dyn Host, cfg: &Config) -> u32 {
    let mut failures = finish_pending(host, cfg) + repair_lane_copies(host, cfg);
    for mut rec in load_all(&cfg.launch_dir) {
        if rec.stopped() || allowed(cfg, &rec) {
            continue;
        }
        let Some(last) = host.activity_ts(&rec) else {
            continue;
        };
        if host.now().saturating_sub(last) < cfg.idle_s {
            continue;
        }
        let head = rec.lane().and_then(|l| {
            parse_status(
                &l,
                &std::fs::read_to_string(status_path(&cfg.pr_dir, &l)).ok()?,
            )
            .head
        });
        stop_session(host, cfg, &mut rec, "idle", head.as_deref(), &mut failures);
    }
    failures
}
