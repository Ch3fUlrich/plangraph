//! A fix round for a native writer owned by another host. The origin routes the prompt through
//! the spawner (`route`); the owning host starts the resume of the saved session detached
//! (`local_cli`) so the route returns a receipt while the round runs.
use pg_worker_core::run::record::run_dir;
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::Path,
    time::Duration,
};

const MAX_PROMPT: u64 = 1024 * 1024;

/// A request id as the requesting host generates it: 8 to 64 hex digits or dashes.
pub(crate) fn valid_request_id(id: &str) -> Option<&str> {
    ((8..=64).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-'))
        .then_some(id)
}

fn generated_request_id() -> String {
    pg_worker_core::run::record::new_run_id().replace("run-", "")
}

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

/// How long the owning host waits for the detached resume to reopen the run before it refuses.
const REOPEN_WAIT: Duration = Duration::from_secs(30);

/// Reserve the next resume round of the run, start it detached and acknowledge only once the
/// resume has reopened the run (it holds the supervisor lock and has bumped `resume_number`).
/// The prompt file belongs to that round alone.
fn start(state: &Path, id: &str, prompt: &str, request_id: Option<&str>) -> Result<Value, String> {
    // Every acknowledgement is bound to a persisted id: a caller that sends none gets one made.
    let generated = generated_request_id();
    let request_id = request_id.unwrap_or(&generated);
    let directory = run_dir(state, id).map_err(|_| "unknown run id")?;
    let record_path = directory.join("record.json");
    let read =
        || crate::records::read_record_file(&record_path).map_err(|_| "unknown run id".to_owned());
    let record = read()?;
    if record["run_id"] != id
        || record["run_kind"] != "native-codex"
        || record["overflow_remote"] == true
        || record["origin_receipt"] == true
    {
        return Err("resume requires a native writer on its owning host".into());
    }
    if !matches!(record["status"].as_str(), Some("completed" | "failed"))
        || !record["ended"].is_number()
        || record["session_id"].as_str().is_none_or(str::is_empty)
    {
        return Err("native writer has no safely ended run to resume".into());
    }
    if prompt.trim().is_empty() {
        return Err("resume prompt must contain between 1 byte and 1 MiB of text".into());
    }
    let round = record["resume_number"].as_u64().unwrap_or(0) + 1;
    let file = directory.join(format!("resume-prompt-{round}.md"));
    store_prompt(&file, prompt).map_err(|e| match e.kind() {
        std::io::ErrorKind::AlreadyExists => format!("resume round {round} is already reserved"),
        _ => "resume prompt not stored".to_owned(),
    })?;
    let started =
        spawn_detached(state, id, &directory, &file, round, request_id).and_then(|mut child| {
            let deadline = std::time::Instant::now() + REOPEN_WAIT;
            loop {
                if let Some(outcome) = read().ok().and_then(|r| reopened(&r, round, request_id)) {
                    return outcome;
                }
                if child
                    .try_wait()
                    .map_err(|_| "resume child unreadable")?
                    .is_some()
                {
                    return Err(failure(&directory, round));
                }
                if std::time::Instant::now() >= deadline {
                    return Err("resume did not reopen the run in time".into());
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        });
    let observed = match started {
        Ok(observed) => observed,
        Err(reason) => {
            let _ = std::fs::remove_file(&file);
            return Err(reason);
        }
    };
    let mut receipt = json!({"run_id":id,"host":record["host"],"ok":true,"resumed":true,
                             "queued":true,"resume_number":observed});
    receipt["request_id"] = json!(request_id);
    Ok(receipt)
}

/// Whether the run record shows round `round` (or later) opened, and by which request: only this
/// request's id confirms it, and the round reported is the one observed.
fn reopened(record: &Value, round: u64, request_id: &str) -> Option<Result<u64, String>> {
    let observed = record["resume_number"].as_u64().filter(|n| *n >= round)?;
    Some(if record["resume_request_id"] == request_id {
        Ok(observed)
    } else {
        Err("another resume request reopened the run".into())
    })
}

/// The last line the failed resume child logged.
fn failure(directory: &Path, round: u64) -> String {
    std::fs::read_to_string(directory.join(format!("resume-{round}.log")))
        .ok()
        .and_then(|log| log.lines().last().map(str::to_owned))
        .filter(|line| !line.is_empty())
        .unwrap_or_else(|| "resume could not reopen the run".into())
}

/// Create the round's prompt file exclusively and write it; a failed write leaves no file, so the
/// same round can be requested again.
fn store_prompt(file: &Path, prompt: &str) -> std::io::Result<()> {
    store_with(file, |out| out.write_all(prompt.as_bytes()))
}

fn store_with(
    file: &Path,
    write: impl FnOnce(&mut std::fs::File) -> std::io::Result<()>,
) -> std::io::Result<()> {
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut out = options.open(file)?;
    let written = write(&mut out);
    drop(out);
    if written.is_err() {
        let _ = std::fs::remove_file(file);
    }
    written
}

#[cfg(unix)]
fn spawn_detached(
    state: &Path,
    id: &str,
    directory: &Path,
    prompt: &Path,
    round: u64,
    request_id: &str,
) -> Result<std::process::Child, String> {
    use std::os::unix::process::CommandExt;
    use std::process::{Command, Stdio};
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(directory.join(format!("resume-{round}.log")))
        .map_err(|_| "resume log not writable")?;
    let exe = std::env::current_exe().map_err(|_| "own executable unknown")?;
    let mut command = Command::new(exe);
    command
        .args(["resume", id, "--state"])
        .arg(state)
        .arg("--prompt-file")
        .arg(prompt);
    command.args(["--request-id", request_id]);
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log)
        .process_group(0)
        .spawn()
        .map_err(|_| "resume could not start".into())
}

#[cfg(not(unix))]
fn spawn_detached(
    _: &Path,
    _: &str,
    _: &Path,
    _: &Path,
    _: u64,
    _: &str,
) -> Result<std::process::Child, String> {
    Err("native writer resume requires the POSIX transport".into())
}

/// `pg-spawnd resume-local RUN_ID [--state PATH]`: the prompt is read from standard input.
pub(crate) fn local_cli(
    args: &[String],
    input: impl Read,
    output: &mut dyn Write,
) -> Result<(), String> {
    let usage = "usage: pg-spawnd resume-local RUN_ID [--state PATH] [--request-id ID]";
    let [id, flags @ ..] = args else {
        return Err(usage.into());
    };
    let (mut state, mut request_id) = (None, None);
    let mut flags = flags.iter().map(String::as_str);
    while let Some(flag) = flags.next() {
        match (flag, flags.next()) {
            ("--state", Some(path)) if state.is_none() => state = Some(path),
            ("--request-id", Some(value)) if request_id.is_none() => {
                request_id = Some(valid_request_id(value).ok_or(usage)?)
            }
            _ => return Err(usage.into()),
        }
    }
    let state = crate::state::resolve_state(state, |key| std::env::var_os(key))?;
    let mut prompt = String::new();
    let reply = input
        .take(MAX_PROMPT + 1)
        .read_to_string(&mut prompt)
        .map_err(|_| "resume prompt is unreadable".to_owned())
        .and_then(|_| {
            if prompt.len() as u64 > MAX_PROMPT {
                Err("resume prompt must contain between 1 byte and 1 MiB of text".to_owned())
            } else {
                start(&state, id, &prompt, request_id)
            }
        })
        .unwrap_or_else(|reason| refusal(id, &reason));
    writeln!(output, "{reply}").map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_round_carrying_this_requests_id_confirms_it() {
        let seen = |n: u64, id: Option<&str>| {
            let mut record = json!({"resume_number": n});
            if let Some(id) = id {
                record["resume_request_id"] = json!(id);
            }
            record
        };
        // Not reopened yet.
        assert_eq!(reopened(&seen(0, None), 1, "aaaaaaaa"), None);
        // The round names this request: its observed round is reported.
        assert_eq!(
            reopened(&seen(1, Some("aaaaaaaa")), 1, "aaaaaaaa"),
            Some(Ok(1))
        );
        assert_eq!(
            reopened(&seen(3, Some("aaaaaaaa")), 1, "aaaaaaaa"),
            Some(Ok(3))
        );
        // The round was opened by request B: request A is never acknowledged.
        assert!(matches!(
            reopened(&seen(1, Some("bbbbbbbb")), 1, "aaaaaaaa"),
            Some(Err(_))
        ));
        assert!(matches!(
            reopened(&seen(1, None), 1, "aaaaaaaa"),
            Some(Err(_))
        ));
    }

    #[test]
    fn a_caller_without_an_id_gets_a_valid_generated_one() {
        let first = generated_request_id();
        assert!(valid_request_id(&first).is_some(), "{first}");
        assert_ne!(first, generated_request_id());
    }

    #[test]
    fn a_failed_prompt_write_releases_the_round_for_a_retry() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("resume-prompt-1.md");
        let failed = store_with(&file, |out| {
            out.write_all(b"x")?;
            Err(std::io::Error::other("disk full"))
        });
        assert!(failed.is_err());
        assert!(!file.exists());
        store_prompt(&file, "second try").unwrap();
        assert_eq!(std::fs::read_to_string(&file).unwrap(), "second try");
        // A stored round is never overwritten.
        let again = store_prompt(&file, "third").unwrap_err();
        assert_eq!(again.kind(), std::io::ErrorKind::AlreadyExists);
        assert_eq!(std::fs::read_to_string(&file).unwrap(), "second try");
    }
}
