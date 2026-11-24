//! `pg-spawnd round-watch`: one watchdog and telemetry pass over every launched session.

use super::{
    round_host::LocalHost,
    round_store,
    round_time::Reader,
    round_watch::{tick, Action},
};
use serde_json::{json, Value};
use std::{fs, path::Path};

/// Transcript of a session: the harness names the folder after the working directory.
pub fn transcript_path(home: &Path, cwd: &str, session_id: &str) -> std::path::PathBuf {
    let folder: String = cwd
        .chars()
        .map(|c| if c == '/' || c == '.' { '-' } else { c })
        .collect();
    home.join(".claude/projects")
        .join(folder)
        .join(format!("{session_id}.jsonl"))
}

fn sessions(state: &Path) -> Vec<Value> {
    let Ok(dir) = fs::read_dir(state.join("launch")) else {
        return Vec::new();
    };
    let mut specs: Vec<Value> = dir
        .flatten()
        .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
        .filter_map(|e| serde_json::from_str(&fs::read_to_string(e.path()).ok()?).ok())
        .filter(|spec: &Value| spec["session_id"].is_string() && spec["cwd"].is_string())
        .collect();
    specs.sort_by_key(|s| s["session_name"].as_str().unwrap_or("").to_owned());
    specs
}

/// Book rows for every session transcript and run the wait cap; returns one JSON line per event.
pub fn run(state: &Path, home: &Path, host: &str, now_ms: i64) -> Vec<Value> {
    let mut log = Vec::new();
    for spec in sessions(state) {
        let (Some(id), Some(cwd)) = (spec["session_id"].as_str(), spec["cwd"].as_str()) else {
            continue;
        };
        let name = spec["session_name"].as_str().unwrap_or(id);
        let Ok(text) = fs::read_to_string(transcript_path(home, cwd, id)) else {
            continue;
        };
        let rows: Vec<Value> = Reader::read(name, &text)
            .rows
            .iter()
            .map(|row| row.to_json(host, name))
            .collect();
        if let Err(error) = round_store::upsert(&state.join("round-telemetry/rows.jsonl"), &rows) {
            log.push(json!({"rule": "round-telemetry-error", "session": name, "error": error.to_string()}));
        }
        let local = LocalHost::new(state.to_path_buf(), id.to_owned());
        let path = state.join("round-watch").join(format!("{name}.json"));
        match tick(name, &text, now_ms, &path, &local) {
            Ok(actions) => log.extend(actions.iter().map(|a| event(name, a))),
            Err(error) => log.push(
                json!({"rule": "round-watch-error", "session": name, "error": error.to_string()}),
            ),
        }
    }
    log
}

fn event(session: &str, action: &Action) -> Value {
    match action {
        Action::Stopped { task_id, command } => {
            json!({"rule": "round-wait-stopped", "session": session, "task": task_id, "command": command})
        }
        Action::StopPending { task_id } => {
            json!({"rule": "round-wait-stop-pending", "session": session, "task": task_id})
        }
        Action::Notified { task_id } => {
            json!({"rule": "round-wait-notified", "session": session, "task": task_id})
        }
        Action::Blocked { round_key, command } => {
            json!({"rule": "round-blocked", "session": session, "round": round_key, "command": command})
        }
    }
}

/// Entry for `pg-spawnd round-watch --state DIR [--now-ms N] [--digest]`.
pub fn main(args: &[String]) -> i32 {
    let mut state = None;
    let mut now = None;
    let mut digest = false;
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--state" => state = it.next().map(std::path::PathBuf::from),
            "--now-ms" => now = it.next().and_then(|n| n.parse::<i64>().ok()),
            "--digest" => digest = true,
            _ => {
                eprintln!("usage: pg-spawnd round-watch --state DIR [--now-ms N] [--digest]");
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
    let home = std::env::var_os("HOME")
        .map(std::path::PathBuf::from)
        .unwrap_or_default();
    let host = super::local_hostname();
    let now = now.unwrap_or_else(|| chrono::Utc::now().timestamp_millis());
    for line in run(&state, &home, &host, now) {
        println!("{line}");
    }
    0
}
