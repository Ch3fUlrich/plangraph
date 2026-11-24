//! pg-session-resume <stop-done|reap|tick|stop-hook> [options]
//!
//!   --state DIR      routing state dir (default $ROUTING_STATE_DIR, else ~/.local/state/routing)
//!   --pr-dir DIR     lane status files (default $PG_PR_DIR, else ~/.local/state/pr)
//!   --idle-min N     reap sessions idle this long (default 30)
//!   --quiet-s N      stop-done: the session must show no activity this long (default 60)
//!   --session ID     stop-done: only this session id (the Stop hook path)
//!   --delay-s N      stop-done: wait first, so the turn that wrote DONE can end

use pg_session_resume::engine::{reap_idle, stop_done, Config, DEFAULT_ALLOW};
use pg_session_resume::lock::Lock;
use pg_session_resume::system::System;
use std::collections::HashMap;
use std::path::PathBuf;

fn home() -> PathBuf {
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
}

fn parse_flags(args: &[String]) -> Result<HashMap<String, String>, String> {
    let mut flags = HashMap::new();
    let mut it = args.iter();
    while let Some(key) = it.next() {
        let name = key
            .strip_prefix("--")
            .ok_or(format!("unexpected argument {key}"))?;
        flags.insert(
            name.to_owned(),
            it.next().ok_or(format!("{key} needs a value"))?.clone(),
        );
    }
    Ok(flags)
}

fn number(flags: &HashMap<String, String>, key: &str, default: u64) -> Result<u64, String> {
    flags.get(key).map_or(Ok(default), |v| {
        v.parse().map_err(|_| format!("--{key} must be a number"))
    })
}

/// Allowlist: the built-in globs plus one glob per line of <state>/launch/stop-allowlist.txt.
fn allowlist(state: &std::path::Path) -> Vec<String> {
    let mut allow: Vec<String> = DEFAULT_ALLOW.iter().map(|s| (*s).to_owned()).collect();
    if let Ok(text) = std::fs::read_to_string(state.join("launch/stop-allowlist.txt")) {
        allow.extend(
            text.lines()
                .map(str::trim)
                .filter(|l| !l.is_empty() && !l.starts_with('#'))
                .map(str::to_owned),
        );
    }
    allow
}

fn stop_hook(state: &std::path::Path, args: &[String]) -> Result<(), String> {
    let mut payload = String::new();
    std::io::Read::read_to_string(&mut std::io::stdin(), &mut payload)
        .map_err(|e| e.to_string())?;
    let value: serde_json::Value = serde_json::from_str(&payload).map_err(|e| e.to_string())?;
    let id = value
        .get("session_id")
        .and_then(|v| v.as_str())
        .ok_or("no session_id in hook input")?;
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let mut child = std::process::Command::new(exe);
    child
        .args([
            "stop-done",
            "--quiet-s",
            "0",
            "--delay-s",
            "3",
            "--session",
            id,
            "--state",
        ])
        .arg(state);
    child.args(args);
    child
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    child.spawn().map(drop).map_err(|e| e.to_string())
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (cmd, rest) = args
        .split_first()
        .ok_or("usage: pg-session-resume <stop-done|reap|tick|stop-hook>")?;
    let flags = parse_flags(rest)?;
    let state = flags.get("state").map(PathBuf::from).unwrap_or_else(|| {
        std::env::var_os("ROUTING_STATE_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join(".local/state/routing"))
    });
    let pr_dir = flags.get("pr-dir").map(PathBuf::from).unwrap_or_else(|| {
        std::env::var_os("PG_PR_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join(".local/state/pr"))
    });
    if cmd == "stop-hook" {
        let passthrough = ["--pr-dir".to_owned(), pr_dir.display().to_string()];
        return stop_hook(&state, &passthrough);
    }
    let cfg = Config {
        launch_dir: state.join("launch/sessions"),
        pr_dir,
        idle_s: number(&flags, "idle-min", 30)? * 60,
        allow: allowlist(&state),
    };
    let mut host = System {
        log_file: state.join("session-lifecycle.log"),
        log_failed: false,
    };
    let quiet = number(&flags, "quiet-s", 60)?;
    let only = flags.get("session").map(String::as_str);
    std::thread::sleep(std::time::Duration::from_secs(number(
        &flags, "delay-s", 0,
    )?));
    // One tick at a time: a second one finds the lock held and leaves the work to the first.
    std::fs::create_dir_all(state.join("launch")).map_err(|e| e.to_string())?;
    let Some(_lock) = Lock::acquire(&state.join("launch/pg-session-resume.lock")) else {
        println!("pg-session-resume: another tick holds the lock");
        return Ok(());
    };
    match cmd.as_str() {
        "stop-done" => stop_done(&mut host, &cfg, quiet, only),
        "reap" => reap_idle(&mut host, &cfg),
        "tick" => {
            stop_done(&mut host, &cfg, quiet, None);
            reap_idle(&mut host, &cfg);
        }
        other => return Err(format!("unknown command {other}")),
    }
    if host.log_failed {
        return Err("a log line could not be written".into());
    }
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("pg-session-resume: {e}");
        std::process::exit(2);
    }
}
