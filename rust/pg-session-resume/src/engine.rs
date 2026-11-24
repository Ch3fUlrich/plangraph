//! The three code paths of SESSION-AUTO-CLOSE: stop at DONE, resume on a findings
//! file, reap idle sessions. Every stop and resume writes one log line.

use crate::agent::{claude_stop, resume_cmd};
use crate::contract::{
    delivered_round, glob_match, list_findings, mtime_secs, parse_status, status_path,
};
use crate::host::Host;
use crate::record::{load_all, Agent, Record};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

/// A findings file bigger than this cannot be one argv element.
pub const MAX_PROMPT_BYTES: u64 = 100_000;

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
    pub min_free_mb: u64,
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

fn head_of_status(cfg: &Config, lane: &str) -> Option<String> {
    parse_status(
        lane,
        &std::fs::read_to_string(status_path(&cfg.pr_dir, lane)).ok()?,
    )
    .head
}

/// Log a refusal once per cause: a record that stays refused must not fill the log every tick.
fn refuse_once(host: &mut dyn Host, rec: &mut Record, key: &str, line: &str, why: &str) {
    if rec.str(key) == Some(why) {
        return;
    }
    rec.set(key, json!(why));
    if let Err(e) = rec.save() {
        host.log(&format!(
            "event=record-save-failed name={} error={e}",
            rec.label()
        ));
    }
    host.log(line);
}

/// The lane's resume file: the lane-side copy of the launch record's resume handle.
fn write_lane_resume(host: &mut dyn Host, cfg: &Config, rec: &Record, lane: &str, state: &str) {
    let body = json!({"lane": lane, "state": state, "session_name": rec.label(), "agent": rec.agent().name(),
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
            let Some(pid) = rec
                .u64("pid")
                .filter(|p| *p > 1)
                .and_then(|p| u32::try_from(p).ok())
            else {
                return Target::Keep(Some("no-process-identity".into()));
            };
            let Some(start) = rec.u64("pid_start") else {
                return Target::Keep(Some("no-process-identity".into()));
            };
            match host.proc_start(pid) {
                Some(now) if now == start => Target::Stop(vec!["kill".into(), pid.to_string()]),
                _ => Target::AlreadyStopped, // gone, or the pid belongs to another process now
            }
        }
    }
}

/// Stop one session and record how to resume it. `delivered_cutoff`: findings files
/// not newer than this are what the session already had. Returns true when stopped.
fn stop_session(
    host: &mut dyn Host,
    cfg: &Config,
    rec: &mut Record,
    reason: &str,
    status_head: Option<&str>,
    delivered_cutoff: u64,
) -> bool {
    let (name, lane) = (rec.label(), rec.lane());
    let Some(handle) = rec.handle().map(str::to_owned) else {
        let line = format!("event=stop-skipped name={name} reason={reason} why=no-resume-handle");
        refuse_once(host, rec, "stop_refused", &line, "no-resume-handle");
        return false;
    };
    let target = stop_target(host, rec);
    let before = host.mem_available_mb();
    match target {
        Target::Keep(why) => {
            if let Some(why) = why {
                let line = format!("event=stop-skipped name={name} reason={reason} why={why}");
                refuse_once(host, rec, "stop_refused", &line, &why);
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
    // A round already sent stays sent, whatever the file's mtime says.
    let round = lane
        .as_deref()
        .map_or(0, |l| delivered_round(&cfg.pr_dir, l, delivered_cutoff))
        .max(rec.resume_round());
    let head = rec
        .str("cwd")
        .and_then(|c| host.git_head(Path::new(c)))
        .or_else(|| status_head.map(str::to_owned));
    rec.set("state", json!("stopped"));
    rec.set("stopped_ts", json!(now));
    rec.set("stop_refused", Value::Null);
    rec.set(
        "resume",
        json!({"agent": rec.agent().name(), "handle": handle, "head": head, "status_head": status_head,
               "round": round, "stopped_ts": now, "reason": reason}),
    );
    if let Err(e) = rec.save() {
        host.log(&format!("event=stop-record-failed name={name} error={e}"));
    }
    if let Some(l) = &lane {
        write_lane_resume(host, cfg, rec, l, "stopped");
    }
    let age_s = age(host, rec);
    host.log(&format!(
        "event=stop name={name} age_s={age_s} reason={reason} ram_freed_mb={freed} handle={handle}"
    ));
    true
}

/// A writer whose lane status says DONE with a head it has not been stopped at yet.
/// `quiet_s`: the session must have shown no activity for that long (0 from the Stop hook).
/// `only`: restrict to the record with this session id (the Stop hook path).
pub fn stop_done(host: &mut dyn Host, cfg: &Config, quiet_s: u64, only: Option<&str>) {
    for mut rec in load_all(&cfg.launch_dir) {
        if rec.stopped() || only.is_some_and(|id| rec.str("session_id") != Some(id)) {
            continue;
        }
        let Some(lane) = rec.lane() else { continue };
        let path = status_path(&cfg.pr_dir, &lane);
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        let facts = parse_status(&lane, &text);
        let Some(head) = facts.head.filter(|_| facts.done) else {
            continue;
        };
        // After a resume the old DONE stays in the file until the writer rewrites it.
        if rec.resume_str("status_head") == Some(head.as_str()) || allowed(cfg, &rec) {
            continue;
        }
        let quiet = host
            .activity_ts(&rec)
            .is_none_or(|t| host.now().saturating_sub(t) >= quiet_s);
        if quiet {
            let cutoff = mtime_secs(&path).unwrap_or_else(|| host.now());
            stop_session(host, cfg, &mut rec, "done", Some(&head), cutoff);
        }
    }
}

/// Only the newest stopped record of a lane may resume, and only while no record of
/// the lane is live: two records for one lane must not both start on one file.
fn eligible(all: &[Record], rec: &Record, lane: &str) -> bool {
    let key = |r: &Record| (r.resume_u64("stopped_ts").unwrap_or(0), r.label());
    let same = |r: &&Record| r.path != rec.path && r.lane().as_deref() == Some(lane);
    !all.iter()
        .filter(same)
        .any(|o| !o.stopped() || (o.resume().is_some() && key(o) > key(rec)))
}

/// A stopped writer with a findings file newer than its recorded round resumes
/// in its own worktree; the prompt is exactly the file text.
pub fn resume_findings(host: &mut dyn Host, cfg: &Config) {
    let all = load_all(&cfg.launch_dir);
    for mut rec in load_all(&cfg.launch_dir) {
        let (Some(lane), true) = (rec.lane(), rec.stopped()) else {
            continue;
        };
        let Some(handle) = rec.resume_str("handle").map(str::to_owned) else {
            continue;
        };
        let Some(found) = list_findings(&cfg.pr_dir, &lane)
            .into_iter()
            .rfind(|f| f.round > rec.resume_round())
        else {
            continue;
        };
        if !eligible(&all, &rec, &lane) {
            continue;
        }
        // The findings review the head the status file names; another head is another session's.
        let status_head = head_of_status(cfg, &lane);
        if rec
            .resume_str("status_head")
            .is_some_and(|h| status_head.as_deref().is_some_and(|s| s != h))
        {
            let line = format!(
                "event=resume-refused name={} lane={lane} round={} why=head-mismatch",
                rec.label(),
                found.round
            );
            refuse_once(host, &mut rec, "resume_refused", &line, "head-mismatch");
            continue;
        }
        resume_one(
            host,
            cfg,
            &mut rec,
            &lane,
            &handle,
            found.round,
            &found.path,
        );
    }
}

fn worktree_label(host: &dyn Host, rec: &Record, cwd: &Path) -> String {
    let dirty = match host.git_dirty(cwd) {
        Some(true) => "dirty",
        Some(false) => "clean",
        None => "unknown",
    };
    let (now, then) = (host.git_head(cwd), rec.resume_str("head"));
    match (now, then) {
        (Some(n), Some(t)) if n != t => format!("{dirty}+moved"),
        (Some(_), Some(_)) => dirty.to_owned(),
        _ if dirty == "unknown" => dirty.to_owned(),
        _ => format!("{dirty}+head-unknown"),
    }
}

fn resume_one(
    host: &mut dyn Host,
    cfg: &Config,
    rec: &mut Record,
    lane: &str,
    handle: &str,
    round: u64,
    file: &Path,
) {
    let name = rec.label();
    let size = std::fs::metadata(file).map(|m| m.len()).unwrap_or(0);
    let prompt = match std::fs::read_to_string(file) {
        Ok(p) if !p.trim().is_empty() && size <= MAX_PROMPT_BYTES => p,
        Ok(p) if !p.trim().is_empty() => {
            rec.set_resume("round", json!(round));
            let _ = rec.save();
            host.log(&format!("event=resume-refused name={name} lane={lane} round={round} bytes={size} why=too-large"));
            return;
        }
        _ => return, // empty or unreadable: not a findings file yet (written atomically by contract)
    };
    let Some(cwd) = rec.str("cwd").map(PathBuf::from).filter(|c| c.is_dir()) else {
        let line =
            format!("event=resume-refused name={name} lane={lane} round={round} why=no-worktree");
        refuse_once(host, rec, "resume_refused", &line, "no-worktree");
        return;
    };
    let free = host.mem_available_mb();
    if free.is_some_and(|f| f < cfg.min_free_mb) {
        host.log(&format!(
            "event=resume-deferred name={name} lane={lane} round={round} why=low-ram free_mb={}",
            free.unwrap_or(0)
        ));
        return;
    }
    let worktree = worktree_label(host, rec, &cwd);
    // Claim the round before the launch: a crash or a second tick can then never send it twice.
    let previous = rec.resume_round();
    rec.set_resume("round", json!(round));
    if let Err(e) = rec.save() {
        host.log(&format!(
            "event=resume-failed name={name} lane={lane} round={round} error=claim not saved: {e}"
        ));
        return;
    }
    let cmd = resume_cmd(rec.agent(), handle, &prompt);
    let started = if cmd.detached {
        host.spawn(&cwd, &cmd.argv).map(Some)
    } else {
        host.run(Some(&cwd), &cmd.argv).map(|_| None)
    };
    let pid = match started {
        Ok(pid) => pid,
        Err(e) => {
            rec.set_resume("round", json!(previous));
            let _ = rec.save();
            host.log(&format!(
                "event=resume-failed name={name} lane={lane} round={round} error={}",
                one_line(&e)
            ));
            return;
        }
    };
    // The resumed process replaces the old one: persist its verified identity, never keep the old pid.
    let start = pid.and_then(|p| host.proc_start(p));
    rec.set(
        "pid",
        pid.filter(|_| start.is_some())
            .map_or(Value::Null, |p| json!(p)),
    );
    rec.set("pid_start", start.map_or(Value::Null, |s| json!(s)));
    rec.set("state", json!("running"));
    rec.set("stopped_ts", Value::Null);
    rec.set("resume_refused", Value::Null);
    let stopped_for = host
        .now()
        .saturating_sub(rec.resume_u64("stopped_ts").unwrap_or(0));
    rec.set_resume("resumed_ts", json!(host.now()));
    if let Err(e) = rec.save() {
        host.log(&format!("event=record-save-failed name={name} error={e}"));
    }
    write_lane_resume(host, cfg, rec, lane, "running");
    let (age_s, free) = (
        age(host, rec),
        free.map_or("unknown".to_owned(), |f| f.to_string()),
    );
    host.log(&format!(
        "event=resume name={name} age_s={age_s} reason=findings lane={lane} round={round} stopped_for_s={stopped_for} ram_freed_mb=0 ram_free_mb={free} worktree={worktree} handle={handle}"
    ));
}

/// Live sessions idle for `idle_s` outside the allowlist. A findings file newer than
/// the last activity is not a reason to keep the session: it is stopped and resumed by it.
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
        let head = rec.lane().and_then(|l| head_of_status(cfg, &l));
        stop_session(host, cfg, &mut rec, "idle", head.as_deref(), last);
    }
}
