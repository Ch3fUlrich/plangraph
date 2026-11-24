//! A fix round for a native writer owned by another host goes through the spawner route; the
//! owning host stores the prompt and starts the resume detached.
#![cfg(unix)]
use serde_json::{json, Value};
use std::{fs, os::unix::fs::PermissionsExt, path::Path, process::Command, time::Duration};

const RUN: &str = "run-fixture1";

fn record(state: &Path, record: Value) {
    let directory = state.join("runs").join(RUN);
    fs::create_dir_all(&directory).unwrap();
    fs::write(directory.join("record.json"), record.to_string()).unwrap();
}

fn pg(_state: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"));
    command.env_remove("PG_SPAWND_ADAPTER");
    command
}

#[test]
fn origin_routes_the_prompt_to_the_owning_host_through_the_spawner() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    record(
        &state,
        json!({"run_id":RUN,"run_kind":"native-codex","overflow_remote":true,"status":"completed"}),
    );
    let adapter = dir.path().join("adapter");
    fs::write(
        &adapter,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > '{0}/argv'\ncat > '{0}/stdin'\nprintf '{{\"run_id\":\"{RUN}\",\"ok\":true,\"resumed\":true,\"queued\":true}}'\n",
            dir.path().display()
        ),
    )
    .unwrap();
    fs::set_permissions(&adapter, fs::Permissions::from_mode(0o700)).unwrap();
    let prompt = dir.path().join("prompt.md");
    fs::write(&prompt, "fix finding one\nfix finding two").unwrap();
    // No --host: the origin receipt alone sends the fix round to the spawner route.
    let out = pg(&state)
        .args(["resume", RUN, "--prompt-file"])
        .arg(&prompt)
        .arg("--state")
        .arg(&state)
        .env("PG_SPAWND_ADAPTER", &adapter)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let receipt: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(receipt["resumed"], true, "{receipt}");
    let argv = fs::read_to_string(dir.path().join("argv")).unwrap();
    assert_eq!(
        argv.lines().collect::<Vec<_>>(),
        ["--state", state.to_str().unwrap(), "resume", RUN]
    );
    assert_eq!(
        fs::read_to_string(dir.path().join("stdin")).unwrap(),
        "fix finding one\nfix finding two"
    );
    // A named host reaches the route even without a local receipt.
    let out = pg(&state)
        .args(["resume", "run-nolocal", "--host", "remote", "--prompt-file"])
        .arg(&prompt)
        .arg("--state")
        .arg(&state)
        .env("PG_SPAWND_ADAPTER", &adapter)
        .output()
        .unwrap();
    assert!(
        !out.status.success(),
        "receipt for another run id is refused"
    );
    let argv = fs::read_to_string(dir.path().join("argv")).unwrap();
    assert!(argv.contains("--host\nremote"), "{argv}");
}

fn resume_local(state: &Path, id: &str, prompt: &str) -> Value {
    use std::io::Write;
    let mut child = pg(state)
        .args(["resume-local", id, "--state"])
        .arg(state)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(prompt.as_bytes())
        .unwrap();
    let out = child.wait_with_output().unwrap();
    serde_json::from_slice(&out.stdout).unwrap()
}

#[test]
fn owning_host_stores_the_prompt_and_starts_the_resume_detached() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    record(
        &state,
        json!({"run_id":RUN,"run_kind":"native-codex","status":"completed","ended":1.0,
               "session_id":"fixture-session","host":"fixture-host"}),
    );
    let receipt = resume_local(&state, RUN, "fix round prompt");
    assert_eq!(
        receipt,
        json!({"run_id":RUN,"host":"fixture-host","ok":true,"resumed":true,"queued":true})
    );
    let file = state.join("runs").join(RUN).join("resume-prompt.md");
    assert_eq!(fs::read_to_string(&file).unwrap(), "fix round prompt");
    assert_eq!(
        fs::metadata(&file).unwrap().permissions().mode() & 0o777,
        0o600
    );
    // The detached `resume` child started: it reports on its own log that it cannot reopen the
    // run (the fixture has no saved request).
    let log = state.join("runs").join(RUN).join("resume.log");
    let mut text = String::new();
    for _ in 0..100 {
        text = fs::read_to_string(&log).unwrap_or_default();
        if !text.is_empty() {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    assert_eq!(text, "unknown run id\n");
}

#[test]
fn owning_host_refuses_receipts_running_runs_and_empty_prompts() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    record(
        &state,
        json!({"run_id":RUN,"run_kind":"native-codex","overflow_remote":true,"status":"completed",
               "ended":1.0,"session_id":"s"}),
    );
    assert_eq!(resume_local(&state, RUN, "p")["ok"], false);
    record(
        &state,
        json!({"run_id":RUN,"run_kind":"native-codex","status":"running","session_id":"s"}),
    );
    assert_eq!(resume_local(&state, RUN, "p")["ok"], false);
    record(
        &state,
        json!({"run_id":RUN,"run_kind":"native-codex","status":"completed","ended":1.0,"session_id":"s"}),
    );
    assert_eq!(resume_local(&state, RUN, "  \n")["ok"], false);
    assert_eq!(resume_local(&state, "run-unknown", "p")["ok"], false);
    assert!(!state
        .join("runs")
        .join(RUN)
        .join("resume-prompt.md")
        .exists());
}

#[path = "../../test_support/python.rs"]
mod test_python;

/// The Python spawner verb sends the prompt on standard input to `pg-spawnd resume-local` on the
/// configured host, over ssh, with the same receipt bracketing as a steer.
#[test]
fn spawner_cli_resume_verb_reaches_resume_local_on_the_host() {
    let state = tempfile::tempdir().unwrap();
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let output = test_python::command()
        .env("PYTHONPATH", source)
        .arg("-c")
        .arg(
            r#"
import contextlib, io, json, re, sys
from types import SimpleNamespace
from unittest.mock import patch
from plangraph.spawner import cli, remote_ps

state = sys.argv[1]
prompt = 'fix round with quotes " \' and\nnewline'
run_id = 'run-fixture'
host = dict(repo=state, ssh='fixture', spawner_src=state, state_dir=state)
routes = {'hosts': {'posix': host}}
seen = []
def ssh(argv, *, input, **kwargs):
    assert argv[0] == 'ssh' and argv[-2] == 'fixture', argv
    assert input.decode() == prompt and prompt not in argv[-1], argv
    assert f'pg-spawnd resume-local {run_id} --state' in argv[-1], argv
    begin, end = re.findall(r'PLANGRAPH_RECEIPT_[a-f0-9]+_(?:BEGIN|END)', argv[-1])
    seen.append(argv[-1])
    receipt = json.dumps(dict(run_id=run_id, ok=True, resumed=True, queued=True))
    return SimpleNamespace(stdout=f'{begin}\n{receipt}\n{end}\n'.encode())

with patch('plangraph.spawner.overflow.registry_caps', return_value={}), \
     patch('plangraph.spawner.overflow.placement_config', return_value=routes), \
     patch.object(remote_ps.subprocess, 'run', side_effect=ssh):
    stdout = io.StringIO()
    with patch('sys.stdin', io.StringIO(prompt)), contextlib.redirect_stdout(stdout):
        assert cli.main(['--state', state, 'resume', run_id, '--host', 'posix']) == 0
    reply = json.loads(stdout.getvalue())
    assert reply['resumed'] and reply['queued'] and len(seen) == 1, (reply, seen)
    # An empty prompt never reaches the host.
    stdout = io.StringIO()
    with patch('sys.stdin', io.StringIO('  ')), contextlib.redirect_stdout(stdout):
        cli.main(['--state', state, 'resume', run_id, '--host', 'posix'])
    assert json.loads(stdout.getvalue())['resumed'] is False and len(seen) == 1
"#,
        )
        .arg(state.path())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
