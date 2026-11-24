//! Stop at DONE and reap idle sessions (SESSION-AUTO-CLOSE). Every stop writes one log
//! line; a refusal is logged once per cause.

use crate::agent::claude_stop;
use crate::contract::{glob_match, parse_status, status_path};
use crate::host::Host;
use crate::record::{load_all, Agent, Record};
use serde_json::{json, Value};
use std::path::PathBuf;

pub const DEFAULT_ALLOW: &[&str] = &[
    "*-L1",
    "*-L1-*",
    "L1",
    "L1-*",
    "routing",
    "routing-*",
    "plan-*",
    "overseer",
    "overseer-*",
    "planner",
    "planner-*",
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
    names
        .iter()
        .flatten()
        .any(|n| cfg.allow.iter().any(|g| glob_match(g, n)))
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

/// Save the record; a failure is logged, never silent.
fn save(host: &mut dyn Host, rec: &Record) {
    if let Err(e) = rec.save() {
        host.log(&format!(
            "event=record-save-failed name={} error={e}",
            rec.label()
        ));
    }
}

/// Log a refusal once per cause: a record that stays refused must not fill the log every tick.
fn refuse_once(host: &mut dyn Host, rec: &mut Record, line: &str, why: &str) {
    if rec.str("stop_refused") == Some(why) {
        return;
    }
    rec.set("stop_refused", json!(why));
    save(host, rec);
    host.log(line);
}

/// The lane's resume file: the lane-side copy of the launch record's resume handle.
fn write_lane_resume(host: &mut dyn Host, cfg: &Config, rec: &Record, lane: &str) {
    let body = json!({"lane": lane, "state": "stopped", "session_name": rec.label(), "agent": rec.agent().name(),
        "handle": rec.resume_str("handle"), "head": rec.resume_str("head"), "round": rec.resume_round(),
        "ts": host.now()});
    let path = cfg.pr_dir.join(format!("{lane}.resume.json"));
    let tmp = path.with_extension("json.tmp");
    let done = std::fs::write(&tmp, body.to_string()).and_then(|_| std::fs::rename(&tmp, &path));
    if let Err(e) = done {
        host.log(&format!(
            "event=lane-record-failed name={} lane={lane} error={e}",
            rec.label()
        ));
    }
}

enum Target {
    /// Run this command to stop the session.
    Stop(Vec<String>),
    /// The runtime already stopped it; only the record is missing.
    AlreadyStopped,
    /// Leave it running; the string says why (None: stay silent).
    Keep(Option<String>),
}

/// What stopping this session means right now, from the runtime's own state.
fn stop_target(host: &dyn Host, rec: &Record) -> Target {
    match rec.agent() {
        Agent::Claude => match host.job_state(rec) {
            None => Target::Keep(Some("no-job-state".into())),
            Some(j) if j.state == "stopped" => Target::AlreadyStopped,
            Some(j) if j.state == "done" && j.tasks == 0 => {
                claude_stop(rec).map_or(Target::Keep(Some("no-stop-target".into())), Target::Stop)
            }
            Some(_) => Target::Keep(None), // working, blocked or with running tasks
        },
        Agent::Codex | Agent::OpenCode => {
            let pid = rec
                .u64("pid")
                .filter(|p| *p > 1)
                .and_then(|p| u32::try_from(p).ok());
            let (Some(pid), Some(start)) = (pid, rec.u64("pid_start")) else {
                return Target::Keep(Some("no-process-identity".into()));
            };
            if host.proc_start(pid) != Some(start) {
                return Target::AlreadyStopped; // gone, or the pid belongs to another process now
            }
            // A model request in flight leaves no child process: only the runtime's own
            // end-of-turn marker is a positive idle signal.
            match (host.turn_ended(rec), host.has_children(pid)) {
                (Some(true), Some(false)) => Target::Stop(vec!["kill".into(), pid.to_string()]),
                (None, _) => Target::Keep(Some("no-idle-signal".into())),
                (_, None) => Target::Keep(Some("no-runtime-state".into())),
                _ => Target::Keep(None),
            }
        }
    }
}

/// Stop one session and record how to resume it. Returns true when stopped.
fn stop_session(
    host: &mut dyn Host,
    cfg: &Config,
    rec: &mut Record,
    reason: &str,
    status_head: Option<&str>,
) -> bool {
    let (name, lane) = (rec.label(), rec.lane());
    let Some(handle) = rec.handle().map(str::to_owned) else {
        let line = format!("event=stop-skipped name={name} reason={reason} why=no-resume-handle");
        refuse_once(host, rec, &line, "no-resume-handle");
        return false;
    };
    let target = stop_target(host, rec);
    let before = host.mem_available_mb();
    match target {
        Target::Keep(why) => {
            if let Some(why) = why {
                let line = format!("event=stop-skipped name={name} reason={reason} why={why}");
                refuse_once(host, rec, &line, &why);
            }
            return false;
        }
        Target::Stop(argv) => {
            if let Err(e) = host.run(None, &argv) {
                host.log(&format!(
                    "event=stop-failed name={name} reason={reason} error={}",
                    one_line(&e)
                ));
                return false;
            }
        }
        Target::AlreadyStopped => {}
    }
    let freed = match (before, host.mem_available_mb()) {
        (Some(b), Some(a)) => a.saturating_sub(b).to_string(),
        _ => "unknown".into(),
    };
    let now = host.now();
    // The worktree as observed at the stop; a failed lookup stays unknown, never a substitute.
    let cwd = rec.str("cwd").map(PathBuf::from);
    let head = cwd.as_deref().and_then(|c| host.git_head(c));
    let worktree = cwd.as_deref().and_then(|c| host.worktree_id(c));
    let round = rec.resume_round();
    // state, stopped_ts and the resume object reach the record in one atomic rename.
    rec.set("state", json!("stopped"));
    rec.set("stopped_ts", json!(now));
    rec.set("stop_refused", Value::Null);
    rec.set(
        "resume",
        json!({"agent": rec.agent().name(), "handle": handle, "head": head, "worktree": worktree,
               "status_head": status_head, "round": round, "stopped_ts": now, "reason": reason}),
    );
    save(host, rec);
    if let Some(l) = &lane {
        write_lane_resume(host, cfg, rec, l);
    }
    let age_s = age(host, rec);
    host.log(&format!(
        "event=stop name={name} age_s={age_s} reason={reason} ram_freed_mb={freed} handle={handle}"
    ));
    true
}

/// A writer whose lane status says DONE with a head. `quiet_s`: the session must have shown
/// no activity for that long (0 from the Stop hook). `only`: restrict to the record with this
/// session id (the Stop hook path).
pub fn stop_done(host: &mut dyn Host, cfg: &Config, quiet_s: u64, only: Option<&str>) {
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
            stop_session(host, cfg, &mut rec, "done", Some(&head));
        }
    }
}

/// Live sessions idle for `idle_s` outside the allowlist.
pub fn reap_idle(host: &mut dyn Host, cfg: &Config) {
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
        stop_session(host, cfg, &mut rec, "idle", head.as_deref());
    }
}
