//! `pg-spawnd round-watch`: one watchdog and telemetry pass over every launched session.

use super::{
    round_host::LocalHost,
    round_reader::Reader,
    round_store,
    round_watch::{self, tick, Action},
};
use serde_json::{json, Value};
use std::{fs, path::Path};

/// Transcript of a session: every non-alphanumeric character of the working directory
/// becomes a hyphen, as the session store names it (`sessions_agents.transcript_path`).
pub fn transcript_path(home: &Path, cwd: &str, session_id: &str) -> std::path::PathBuf {
    let folder: String = cwd
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();
    home.join(".claude/projects")
        .join(folder)
        .join(format!("{session_id}.jsonl"))
}

/// Fleet launch records live under the routing state directory, not the spawn state.
pub fn routing_state_dir() -> std::path::PathBuf {
    std::env::var_os("ROUTING_STATE_DIR")
        .filter(|v| !v.is_empty())
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            std::env::home_dir()
                .unwrap_or_default()
                .join(".local/state/routing")
        })
}

/// One snapshot per launched writer session: a replacement launch of the same role does not
/// hide the session it replaced.
fn sessions(routing: &Path) -> Vec<Value> {
    let Ok(dir) = fs::read_dir(routing.join("launch/sessions")) else {
        return Vec::new();
    };
    let mut specs: Vec<Value> = dir
        .flatten()
        .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
        .filter_map(|e| serde_json::from_str(&fs::read_to_string(e.path()).ok()?).ok())
        .filter(|spec: &Value| spec["session_id"].is_string() && spec["cwd"].is_string())
        // Reviews and planning sessions wait on purpose; only writer rounds are measured.
        .filter(|spec: &Value| spec["name"] == "writer")
        .collect();
    specs.sort_by_key(|s| s["session_name"].as_str().unwrap_or("").to_owned());
    specs
}

/// Book rows for every session transcript on this host and run the wait cap.
///
/// A launch record that another host mirrored has no transcript and no process here:
/// it books nothing and stops nothing. Only a command whose owner was recorded on this
/// host is ever signalled.
pub fn run(
    state: &Path,
    routing: &Path,
    home: &Path,
    host: &str,
    now_ms: i64,
    act: bool,
) -> Vec<Value> {
    run_with_host(state, routing, home, host, now_ms, act, |id| {
        LocalHost::new(state.to_path_buf(), id.to_owned())
    })
}

pub(super) fn run_with_host(
    state: &Path,
    routing: &Path,
    home: &Path,
    host: &str,
    now_ms: i64,
    act: bool,
    local_host: impl Fn(&str) -> LocalHost,
) -> Vec<Value> {
    let mut log = Vec::new();
    for spec in sessions(routing) {
        let (Some(id), Some(cwd)) = (spec["session_id"].as_str(), spec["cwd"].as_str()) else {
            continue;
        };
        let name = spec["session_name"].as_str().unwrap_or(id);
        let Ok(text) = fs::read_to_string(transcript_path(home, cwd, id)) else {
            continue;
        };
        let path = round_watch::state_file(state, name);
        if act {
            let local = local_host(id);
            match tick(name, &text, now_ms, &path, &local) {
                Ok(actions) => log.extend(actions.iter().map(|a| event(name, a))),
                Err(error) => log.push(
                    json!({"rule": "round-watch-error", "session": name, "error": error.to_string()}),
                ),
            }
        }
        let blocked = round_watch::blocked_outcome(&path);
        let rows: Vec<Value> = Reader::read_closed(name, &text, round_watch::closed_rounds(&path))
            .rows
            .iter_mut()
            .map(|row| {
                if blocked
                    .as_ref()
                    .is_some_and(|b| b["round_key"] == row.round_key)
                {
                    row.outcome = Some("BLOCKED".into());
                }
                let mut json = row.to_json(host, name);
                if let Some(closed) = blocked
                    .as_ref()
                    .filter(|b| b["round_key"] == row.round_key)
                    .and_then(|b| b["closed_ms"].as_i64())
                {
                    json["closed_ms"] = serde_json::json!(closed);
                }
                json
            })
            .collect();
        if let Err(error) = round_store::upsert(&state.join("round-telemetry/rows.jsonl"), &rows) {
            log.push(json!({"rule": "round-telemetry-error", "session": name, "error": error.to_string()}));
        }
    }
    ingest_native_runs(state, host, &mut log);
    log
}

/// Native Codex writer runs book their rows from the stage intervals of their own record.
fn ingest_native_runs(state: &Path, host: &str, log: &mut Vec<Value>) {
    let Ok(dir) = fs::read_dir(state.join("runs")) else {
        return;
    };
    let rows: Vec<Value> = dir
        .flatten()
        .filter_map(|e| {
            serde_json::from_str::<Value>(&fs::read_to_string(e.path().join("record.json")).ok()?)
                .ok()
        })
        .flat_map(|record| {
            let run = record["run_id"].as_str().unwrap_or("").to_owned();
            super::round_native::rows(&record)
                .iter()
                .map(|row| row.to_json(host, &run))
                .collect::<Vec<_>>()
        })
        .collect();
    if rows.is_empty() {
        return;
    }
    if let Err(error) = round_store::upsert(&state.join("round-telemetry/rows.jsonl"), &rows) {
        log.push(json!({"rule": "round-telemetry-error", "session": "native", "error": error.to_string()}));
    }
}

fn event(session: &str, action: &Action) -> Value {
    match action {
        Action::Stopped { task_id, command } => {
            json!({"rule": "round-wait-stopped", "session": session, "task": task_id, "command": super::round_state::redacted(command)})
        }
        Action::Finished { task_id } => {
            json!({"rule": "round-wait-finished", "session": session, "task": task_id})
        }
        Action::OwnerUnknown { task_id } => {
            json!({"rule": "round-wait-owner-unknown", "session": session, "task": task_id})
        }
        Action::NoticesOverdue { round_key } => {
            json!({"rule": "round-notices-overdue", "session": session, "round": round_key})
        }
        Action::RoundEnded { round_key } => {
            json!({"rule": "round-session-ended", "session": session, "round": round_key})
        }
        Action::StopPending { task_id } => {
            json!({"rule": "round-wait-stop-pending", "session": session, "task": task_id})
        }
        Action::Notified { task_id } => {
            json!({"rule": "round-wait-notified", "session": session, "task": task_id})
        }
        Action::Blocked { round_key, command } => {
            json!({"rule": "round-blocked", "session": session, "round": round_key, "command": super::round_state::redacted(command)})
        }
    }
}

/// Entry for `pg-spawnd round-watch --state DIR [--routing-state DIR] [--now-ms N] [--digest] [--telemetry-only]`.
pub fn main(args: &[String]) -> i32 {
    let mut state = None;
    let mut now = None;
    let mut digest = false;
    let mut act = true;
    let mut routing = None;
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--state" => state = it.next().map(std::path::PathBuf::from),
            "--now-ms" => now = it.next().and_then(|n| n.parse::<i64>().ok()),
            "--routing-state" => routing = it.next().map(std::path::PathBuf::from),
            "--digest" => digest = true,
            "--telemetry-only" => act = false,
            _ => {
                eprintln!("usage: pg-spawnd round-watch --state DIR [--routing-state DIR] [--now-ms N] [--digest] [--telemetry-only]");
                return 2;
            }
        }
    }
    let Some(state) = state else {
        eprintln!("round-watch: --state is required");
        return 2;
    };
    if digest {
        for entry in round_store::digest(&round_store::read(
            &state.join("round-telemetry/rows.jsonl"),
        )) {
            println!("{}", round_store::digest_line(&entry));
        }
        return 0;
    }
    let routing = routing.unwrap_or_else(routing_state_dir);
    let home = std::env::var_os("HOME")
        .map(std::path::PathBuf::from)
        .unwrap_or_default();
    let host = super::local_hostname();
    let now = now.unwrap_or_else(|| chrono::Utc::now().timestamp_millis());
    for line in run(&state, &routing, &home, &host, now, act) {
        println!("{line}");
    }
    0
}
