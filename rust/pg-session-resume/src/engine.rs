//! Stop at DONE and reap idle sessions (SESSION-AUTO-CLOSE). Every stop writes one log
//! line; a refusal is logged once per cause.

use crate::agent::claude_stop;
use crate::contract::{glob_match, parse_status, status_path};
use crate::host::{Host, Process};
use crate::persist::{
    drop_pending, pending_path, phase, read_pending, repair_lane_copies, snapshot,
    write_lane_resume, write_pending, ATTEMPT, LOGGED, STOPPED,
};
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

/// The record holds the stop: lane copy, then the log line, then the sidecar goes. The line is
/// written once: the sidecar says `logged` before it is removed, and a log line that cannot be
/// appended keeps it at `stopped`, so the next tick writes the line.
fn complete_stop(
    host: &mut dyn Host,
    cfg: &Config,
    rec: &Record,
    snap: &mut Value,
    freed: &str,
    failures: &mut u32,
) {
    if let Some(l) = rec.lane() {
        if !write_lane_resume(host, cfg, rec, &l) {
            *failures += 1;
        }
    }
    if phase(snap) != LOGGED {
        let reason = snap["reason"].as_str().unwrap_or_default();
        let handle = snap["handle"].as_str().unwrap_or_default();
        let at = snap["stopped_ts"].as_u64().unwrap_or_default();
        // The stop's identity in the log: the handle and the moment of the stop.
        let id = format!("handle={handle} stopped_ts={at}");
        let line = format!(
            "event=stop name={} age_s={} reason={reason} ram_freed_mb={freed} {id}",
            rec.label(),
            age(host, rec)
        );
        // A line that is out already (the `logged` phase could not be written) is not repeated.
        let known = host.log_has(&["event=stop ", &id]);
        if !known && !host.log(&line) {
            return; // the sidecar stays: the next tick appends it
        }
        if write_pending(rec, snap, LOGGED).is_err() {
            *failures += 1; // the line is out; a repeat is possible, never a loss
            return;
        }
    }
    drop_pending(host, rec, failures);
}

/// Publish a carried-out stop from its snapshot: state, stopped_ts and the resume object reach
/// the record in one atomic rename (the launch identity scopes the stop, see Record::stopped).
/// A record that cannot be saved is not published; its sidecar stays.
fn publish_stop(
    host: &mut dyn Host,
    cfg: &Config,
    rec: &mut Record,
    snap: &mut Value,
    freed: &str,
    failures: &mut u32,
) {
    let mut resume = snap.clone();
    resume.as_object_mut().map(|o| o.remove("phase"));
    // An attempt whose outcome was never written down: the runtime shows the session stopped,
    // the snapshot is the best account of that moment.
    if phase(snap) == ATTEMPT {
        resume["unconfirmed"] = json!(true);
    }
    rec.set("state", json!("stopped"));
    rec.set("stopped_ts", resume["stopped_ts"].clone());
    rec.set("stop_refused", Value::Null);
    rec.set("resume", resume);
    if save(host, rec) {
        complete_stop(host, cfg, rec, snap, freed, failures);
    } else {
        *failures += 1;
    }
}

/// Finish the stops the sidecars name. A sidecar at `stopped` is evidence that the stop command
/// succeeded and is finished whatever the runtime reports now. An `attempt` is ambiguous: its
/// stop may have succeeded before the write that would say so failed, or never happened. The
/// runtime decides: stopped, it is published as `unconfirmed` (the session is resumable, the
/// snapshot is the best account of the stop); still running, the attempt failed and is removed;
/// unreadable, it is kept and reported until the runtime can be read.
fn finish_pending(host: &mut dyn Host, cfg: &Config) -> u32 {
    let mut failures = 0;
    for mut rec in load_all(&cfg.launch_dir) {
        if !pending_path(&rec).exists() {
            continue;
        }
        let Some(mut snap) = read_pending(&rec) else {
            drop_pending(host, &rec, &mut failures); // another launch's, or unreadable
            continue;
        };
        let held = rec.stopped() && rec.resume_u64("stopped_ts") == snap["stopped_ts"].as_u64();
        match (phase(&snap), rec.stopped()) {
            (STOPPED, false) => {
                publish_stop(host, cfg, &mut rec, &mut snap, "unknown", &mut failures)
            }
            (STOPPED | LOGGED, true) if held => {
                complete_stop(host, cfg, &rec, &mut snap, "unknown", &mut failures)
            }
            (ATTEMPT, false) => match stop_target(host, &rec) {
                Target::AlreadyStopped => {
                    publish_stop(host, cfg, &mut rec, &mut snap, "unknown", &mut failures)
                }
                Target::Keep(why) if why == "no-runtime-state" || why == "no-job-state" => {
                    let name = rec.label();
                    let line = format!("event=stop-unconfirmed name={name} why={why}");
                    refuse_once(host, &mut rec, &line, "stop-unconfirmed");
                }
                _ => drop_pending(host, &rec, &mut failures),
            },
            _ => drop_pending(host, &rec, &mut failures),
        }
    }
    failures
}

/// How long the session must have been quiet before it is stopped (`secs`), and whether a
/// session without any activity signal counts as quiet (the DONE path) or never does (reaping).
#[derive(Clone, Copy)]
struct Quiet {
    secs: u64,
    unknown_is_quiet: bool,
}

fn quiet_enough(host: &dyn Host, rec: &Record, q: Quiet) -> bool {
    host.activity_ts(rec).map_or(q.unknown_is_quiet, |t| {
        host.now().saturating_sub(t) >= q.secs
    })
}

/// Stop one session and record how to resume it. `failures` counts what could not be
/// persisted: the run then exits non-zero.
fn stop_session(
    host: &mut dyn Host,
    cfg: &Config,
    rec: &mut Record,
    (reason, status_head, quiet): (&str, Option<&str>, Quiet),
    failures: &mut u32,
) {
    let name = rec.label();
    let Some(handle) = rec.handle().map(str::to_owned) else {
        let line = format!("event=stop-skipped name={name} reason={reason} why=no-resume-handle");
        refuse_once(host, rec, &line, "no-resume-handle");
        return;
    };
    let before = host.mem_available_mb();
    if let Target::Keep(why) = stop_target(host, rec) {
        let line = format!("event=stop-skipped name={name} reason={reason} why={why}");
        refuse_once(host, rec, &line, &why);
        return;
    }
    let mut snap = snapshot(host, rec, &handle, reason, status_head);
    if let Err(e) = write_pending(rec, &mut snap, ATTEMPT) {
        host.log(&format!(
            "event=record-save-failed name={name} error={}",
            one_line(&e.to_string())
        ));
        *failures += 1;
        return; // nothing is stopped that cannot be remembered
    }
    // The snapshot took time: look at the runtime, and at the clock, again right before the
    // session is touched.
    // The clock first, the runtime after it: work that began before the runtime is read shows
    // there, and a session that was quiet by a stale reading shows in the clock.
    let quiet_now = quiet_enough(host, rec, quiet);
    let target = stop_target(host, rec);
    let why = match &target {
        Target::Keep(why) => Some(why.as_str()),
        _ if !quiet_now => Some("activity-resumed"),
        _ => None,
    };
    if let Some(why) = why {
        let line = format!("event=stop-skipped name={name} reason={reason} why={why}");
        drop_pending(host, rec, failures);
        refuse_once(host, rec, &line, why);
        return;
    }
    let acted = match target {
        Target::Stop(argv) => host.run(None, &argv).map(drop),
        Target::Signal(pid, start) => host.terminate(pid, start),
        _ => Ok(()),
    };
    if let Err(e) = acted {
        host.log(&format!(
            "event=stop-failed name={name} reason={reason} error={}",
            one_line(&e)
        ));
        drop_pending(host, rec, failures); // the session still runs: nothing to finish later
        return;
    }
    // The evidence that this stop happened, before anything else can fail.
    if let Err(e) = write_pending(rec, &mut snap, STOPPED) {
        host.log(&format!(
            "event=record-save-failed name={name} error={}",
            one_line(&e.to_string())
        ));
        *failures += 1;
    }
    let freed = match (before, host.mem_available_mb()) {
        (Some(b), Some(a)) => a.saturating_sub(b).to_string(),
        _ => "unknown".into(),
    };
    publish_stop(host, cfg, rec, &mut snap, &freed, failures);
}

/// A writer whose lane status says DONE with a head. `quiet_s`: the session must have shown
/// no activity for that long (0 from the Stop hook). `only`: restrict to the record with this
/// session id (the Stop hook path). Returns the number of records or lane files that could
/// not be written.
pub fn stop_done(host: &mut dyn Host, cfg: &Config, quiet_s: u64, only: Option<&str>) -> u32 {
    let mut failures = finish_pending(host, cfg) + repair_lane_copies(host, cfg);
    let quiet = Quiet {
        secs: quiet_s,
        unknown_is_quiet: true,
    };
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
        if allowed(cfg, &rec) || !quiet_enough(host, &rec, quiet) {
            continue;
        }
        let args = ("done", Some(head.as_str()), quiet);
        stop_session(host, cfg, &mut rec, args, &mut failures);
    }
    failures
}

/// Live sessions idle for `idle_s` outside the allowlist; the count as for `stop_done`.
pub fn reap_idle(host: &mut dyn Host, cfg: &Config) -> u32 {
    let mut failures = finish_pending(host, cfg) + repair_lane_copies(host, cfg);
    let quiet = Quiet {
        secs: cfg.idle_s,
        unknown_is_quiet: false,
    };
    for mut rec in load_all(&cfg.launch_dir) {
        if rec.stopped() || allowed(cfg, &rec) || !quiet_enough(host, &rec, quiet) {
            continue;
        }
        let head = rec.lane().and_then(|l| {
            parse_status(
                &l,
                &std::fs::read_to_string(status_path(&cfg.pr_dir, &l)).ok()?,
            )
            .head
        });
        let args = ("idle", head.as_deref(), quiet);
        stop_session(host, cfg, &mut rec, args, &mut failures);
    }
    failures
}
