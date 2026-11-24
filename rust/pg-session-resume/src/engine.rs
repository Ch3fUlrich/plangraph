//! The three code paths of SESSION-AUTO-CLOSE: stop at DONE, resume on a findings
//! file, reap idle sessions. Every stop and resume writes one log line.

use crate::agent::{resume_cmd, stop_cmd};
use crate::contract::{glob_match, highest_round, list_findings, parse_status, status_path};
use crate::host::Host;
use crate::record::{load_all, Record};
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

fn pending_findings(cfg: &Config, rec: &Record, lane: &str) -> bool {
    highest_round(&cfg.pr_dir, lane) > rec.resume_round()
}

/// Stop one session and record how to resume it. Returns true when it was stopped.
fn stop_session(
    host: &mut dyn Host,
    cfg: &Config,
    rec: &mut Record,
    reason: &str,
    head: Option<&str>,
) -> bool {
    let (name, lane) = (rec.label(), rec.lane());
    let Some(handle) = rec.handle().map(str::to_owned) else {
        host.log(&format!(
            "event=stop-skipped name={name} reason={reason} why=no-resume-handle"
        ));
        return false;
    };
    let Some(argv) = stop_cmd(rec) else {
        host.log(&format!(
            "event=stop-skipped name={name} reason={reason} why=no-stop-target"
        ));
        return false;
    };
    let before = host.mem_available_mb();
    if let Err(e) = host.run(None, &argv) {
        host.log(&format!(
            "event=stop-failed name={name} reason={reason} error={}",
            one_line(&e)
        ));
        return false;
    }
    let freed = match (before, host.mem_available_mb()) {
        (Some(b), Some(a)) => a.saturating_sub(b).to_string(),
        _ => "unknown".into(),
    };
    let now = host.now();
    let round = lane.as_deref().map_or(0, |l| highest_round(&cfg.pr_dir, l));
    rec.set("state", json!("stopped"));
    rec.set("stopped_ts", json!(now));
    rec.set(
        "resume",
        json!({"agent": rec.agent().name(), "handle": handle, "head": head, "round": round,
               "stopped_ts": now, "reason": reason}),
    );
    if let Err(e) = rec.save() {
        host.log(&format!("event=stop-record-failed name={name} error={e}"));
    }
    let age_s = age(host, rec);
    host.log(&format!(
        "event=stop name={name} age_s={age_s} reason={reason} ram_freed_mb={freed} handle={handle}"
    ));
    true
}

fn one_line(s: &str) -> String {
    s.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(200)
        .collect()
}

/// A writer whose lane status says DONE with a head it has not been stopped at yet.
/// `quiet_s`: the transcript must have been still that long (0 from the Stop hook).
/// `only`: restrict to the record with this session id (the Stop hook path).
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
        // After a resume the old DONE stays in the file until the writer rewrites it.
        if rec.resume_str("head") == Some(head.as_str()) || allowed(cfg, &rec) {
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

/// A stopped writer with a findings file newer than its recorded round resumes
/// in its own worktree; the prompt is exactly the file text.
pub fn resume_findings(host: &mut dyn Host, cfg: &Config) {
    for mut rec in load_all(&cfg.launch_dir) {
        let (Some(lane), true) = (rec.lane(), rec.stopped()) else {
            continue;
        };
        let Some(handle) = rec.resume_str("handle").map(str::to_owned) else {
            continue;
        };
        let Some((round, file)) = list_findings(&cfg.pr_dir, &lane)
            .into_iter()
            .rfind(|(n, _)| *n > rec.resume_round())
        else {
            continue;
        };
        resume_one(host, cfg, &mut rec, &lane, &handle, round, &file);
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
            consume(host, rec, round, "refused-too-large");
            host.log(&format!(
                "event=resume-refused name={name} lane={lane} round={round} bytes={size}"
            ));
            return;
        }
        _ => return, // empty or unreadable: not a findings file yet (written atomically by contract)
    };
    let Some(cwd) = rec.str("cwd").map(PathBuf::from).filter(|c| c.is_dir()) else {
        host.log(&format!(
            "event=resume-refused name={name} lane={lane} round={round} why=no-worktree"
        ));
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
    let mut flags = Vec::new();
    if host.git_dirty(&cwd) == Some(true) {
        flags.push("dirty");
    }
    let head_now = host.git_head(&cwd);
    if rec
        .resume_str("head")
        .is_some_and(|h| head_now.as_deref() != Some(h))
    {
        flags.push("moved");
    }
    let cmd = resume_cmd(rec.agent(), handle, &prompt);
    let started = if cmd.detached {
        host.spawn(&cwd, &cmd.argv)
    } else {
        host.run(Some(&cwd), &cmd.argv).map(|_| ())
    };
    if let Err(e) = started {
        host.log(&format!(
            "event=resume-failed name={name} lane={lane} round={round} error={}",
            one_line(&e)
        ));
        return;
    }
    rec.set("state", json!("running"));
    rec.set("stopped_ts", Value::Null);
    rec.set_resume("resumed_ts", json!(host.now()));
    consume(host, rec, round, "resumed");
    let age_s = age(host, rec);
    let stopped_at = rec
        .resume()
        .and_then(|r| r.get("stopped_ts")?.as_u64())
        .unwrap_or(0);
    let stopped_for = host.now().saturating_sub(stopped_at);
    let worktree = if flags.is_empty() {
        "clean".to_owned()
    } else {
        flags.join("+")
    };
    host.log(&format!(
        "event=resume name={name} age_s={age_s} reason=findings lane={lane} round={round} stopped_for_s={stopped_for} worktree={worktree} handle={handle}"
    ));
}

fn consume(host: &mut dyn Host, rec: &mut Record, round: u64, _why: &str) {
    rec.set_resume("round", json!(round));
    if let Err(e) = rec.save() {
        host.log(&format!(
            "event=record-save-failed name={} error={e}",
            rec.label()
        ));
    }
}

/// Live sessions idle for `idle_s` with no findings waiting, outside the allowlist.
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
        let lane = rec.lane();
        if lane
            .as_deref()
            .is_some_and(|l| pending_findings(cfg, &rec, l))
        {
            continue;
        }
        let head = lane.and_then(|l| {
            let facts = parse_status(
                &l,
                &std::fs::read_to_string(status_path(&cfg.pr_dir, &l)).ok()?,
            );
            facts.head
        });
        stop_session(host, cfg, &mut rec, "idle", head.as_deref());
    }
}
