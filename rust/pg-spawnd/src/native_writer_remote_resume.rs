//! A fix round for a native writer owned by another host. The origin routes the prompt through
//! the spawner (`route`); the owning host starts the resume of the saved session detached
//! (`local_cli`) so the route returns a receipt while the round runs.
use pg_worker_core::run::{record::run_dir, record_io::atomic_write_private};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::Path,
    time::Duration,
};

const MAX_PROMPT: u64 = 1024 * 1024;
const PROMPT_FILE: &str = "resume-prompt.md";

fn refusal(id: &str, error: &str) -> Value {
    json!({"run_id":id,"ok":false,"resumed":false,"error":error})
}

/// True when the run's local record is only an origin receipt of a run owned by another host.
pub(crate) fn is_origin_receipt(state: &Path, id: &str) -> bool {
    let Ok(directory) = run_dir(state, id) else {
        return false;
    };
    crate::records::read_record_file(&directory.join("record.json"))
        .is_ok_and(|r| r["overflow_remote"] == true || r["origin_receipt"] == true)
}

/// Send the fix-round prompt to the host that owns the run and print its receipt.
pub(crate) fn route(
    state: &Path,
    id: &str,
    host: Option<&str>,
    prompt: &str,
    out: &mut dyn Write,
) -> Result<(), String> {
    let mut argv = vec!["resume".to_owned(), id.to_owned()];
    if let Some(host) = host {
        argv.extend(["--host".into(), host.into()]);
    }
    let (_, stdout, _) = crate::adapter::Adapter(state)
        .run_input(&argv, Duration::from_secs(60), Some(prompt))
        .map_err(|e| e.into_error_data().message.into_owned())?;
    let reply: Value = serde_json::from_slice(&stdout)
        .map_err(|_| "resume host route returned no receipt".to_owned())?;
    if reply["run_id"] != id || !reply["ok"].is_boolean() {
        return Err("resume host route returned an invalid receipt".into());
    }
    writeln!(out, "{reply}").map_err(|e| e.to_string())?;
    match reply["ok"].as_bool() {
        Some(true) => Ok(()),
        _ => Err(reply["error"]
            .as_str()
            .unwrap_or("resume host route failed")
            .into()),
    }
}

fn start(state: &Path, id: &str, prompt: &str) -> Result<Value, &'static str> {
    let directory = run_dir(state, id).map_err(|_| "unknown run id")?;
    let record = crate::records::read_record_file(&directory.join("record.json"))
        .map_err(|_| "unknown run id")?;
    if record["run_id"] != id
        || record["run_kind"] != "native-codex"
        || record["overflow_remote"] == true
        || record["origin_receipt"] == true
    {
        return Err("resume requires a native writer on its owning host");
    }
    if !matches!(record["status"].as_str(), Some("completed" | "failed"))
        || !record["ended"].is_number()
        || record["session_id"].as_str().is_none_or(str::is_empty)
    {
        return Err("native writer has no safely ended run to resume");
    }
    if prompt.trim().is_empty() {
        return Err("resume prompt must contain between 1 byte and 1 MiB of text");
    }
    let file = directory.join(PROMPT_FILE);
    atomic_write_private(&file, prompt.as_bytes()).map_err(|_| "resume prompt not stored")?;
    spawn_detached(state, id, &directory, &file)?;
    Ok(json!({"run_id":id,"host":record["host"],"ok":true,"resumed":true,"queued":true}))
}

#[cfg(unix)]
fn spawn_detached(
    state: &Path,
    id: &str,
    directory: &Path,
    prompt: &Path,
) -> Result<(), &'static str> {
    use std::os::unix::process::CommandExt;
    use std::process::{Command, Stdio};
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(directory.join("resume.log"))
        .map_err(|_| "resume log not writable")?;
    let exe = std::env::current_exe().map_err(|_| "own executable unknown")?;
    Command::new(exe)
        .args(["resume", id, "--state"])
        .arg(state)
        .arg("--prompt-file")
        .arg(prompt)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log)
        .process_group(0)
        .spawn()
        .map(drop)
        .map_err(|_| "resume could not start")
}

#[cfg(not(unix))]
fn spawn_detached(_: &Path, _: &str, _: &Path, _: &Path) -> Result<(), &'static str> {
    Err("native writer resume requires the POSIX transport")
}

/// `pg-spawnd resume-local RUN_ID [--state PATH]`: the prompt is read from standard input.
pub(crate) fn local_cli(
    args: &[String],
    input: impl Read,
    output: &mut dyn Write,
) -> Result<(), String> {
    let (id, state) = match args {
        [id] => (id, None),
        [id, flag, path] if flag == "--state" => (id, Some(path.as_str())),
        _ => return Err("usage: pg-spawnd resume-local RUN_ID [--state PATH]".into()),
    };
    let state = crate::state::resolve_state(state, |key| std::env::var_os(key))?;
    let mut prompt = String::new();
    let reply = input
        .take(MAX_PROMPT + 1)
        .read_to_string(&mut prompt)
        .map_err(|_| "resume prompt is unreadable")
        .and_then(|_| {
            if prompt.len() as u64 > MAX_PROMPT {
                Err("resume prompt must contain between 1 byte and 1 MiB of text")
            } else {
                start(&state, id, &prompt)
            }
        })
        .unwrap_or_else(|reason| refusal(id, reason));
    writeln!(output, "{reply}").map_err(|e| e.to_string())
}
