//! A fix round for a native writer owned by another host goes through the spawner route; the
//! owning host stores the prompt and starts the resume detached.
#![cfg(unix)]
use serde_json::{json, Value};
use std::{fs, os::unix::fs::PermissionsExt, path::Path, process::Command};

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
fn owning_host_acknowledges_only_a_resume_that_reopened_the_run() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    record(
        &state,
        json!({"run_id":RUN,"run_kind":"native-codex","status":"completed","ended":1.0,
               "session_id":"fixture-session","host":"fixture-host"}),
    );
    // The fixture has no saved request, so the detached resume cannot reopen the run: the
    // round is refused with the child's own reason and its prompt is not left behind.
    let receipt = resume_local(&state, RUN, "fix round prompt");
    assert_eq!(
        receipt,
        json!({"run_id":RUN,"ok":false,"resumed":false,"error":"unknown run id"})
    );
    let directory = state.join("runs").join(RUN);
    assert!(!directory.join("resume-prompt-1.md").exists());
    assert_eq!(
        fs::read_to_string(directory.join("resume-1.log")).unwrap(),
        "unknown run id\n"
    );
    // A prompt already reserved for the round is never overwritten.
    fs::write(directory.join("resume-prompt-1.md"), "first").unwrap();
    let receipt = resume_local(&state, RUN, "second");
    assert_eq!(receipt["error"], "resume round 1 is already reserved");
    assert_eq!(
        fs::read_to_string(directory.join("resume-prompt-1.md")).unwrap(),
        "first"
    );
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
    request_id = re.search(r'--request-id ([0-9a-f]{32})', argv[-1]).group(1)
    receipt = json.dumps(dict(run_id=run_id, ok=True, resumed=True, queued=True, request_id=request_id))
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

/// After a routed fix round the origin receipt stays running until the target reports that round:
/// `result` never returns the earlier terminal head.
#[test]
fn origin_receipt_follows_the_requested_resume_round() {
    let state = tempfile::tempdir().unwrap();
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let output = test_python::command()
        .env("PYTHONPATH", source)
        .arg("-c")
        .arg(
            r#"
import asyncio, contextlib, io, json, pathlib, re, sys
from types import SimpleNamespace
from unittest.mock import patch
from plangraph.spawner import cli, remote_ps
from plangraph.spawner.service import SpawnService

state = pathlib.Path(sys.argv[1])
run_id = 'run-fixture1'
base = dict(run_id=run_id, run_kind='native-codex', role='implement', host='posix')
origin = dict(base, status='completed', ended=10.0, started=1.0, overflow_remote=True,
              origin_receipt=True, final_head='old-head', resume_number=0)
(state / 'runs' / run_id).mkdir(parents=True)
(state / 'runs' / run_id / 'record.json').write_text(json.dumps(origin))
host = dict(repo=str(state), ssh='fixture', spawner_src=str(state), state_dir=str(state))
routes = {'hosts': {'posix': host}}
target = [dict(base, status='completed', ended=10.0, final_head='old-head', resume_number=0)]
def ssh(argv, *, input=None, **kwargs):
    begin, end = re.findall(r'PLANGRAPH_RECEIPT_[a-f0-9]+_(?:BEGIN|END)', argv[-1])
    if 'resume-local' in argv[-1]:
        request_id = re.search(r'--request-id ([0-9a-f]{32})', argv[-1]).group(1)
        body = dict(run_id=run_id, ok=True, resumed=True, queued=True, resume_number=1,
                    request_id=request_id)
        return SimpleNamespace(stdout=f'{begin}\n{json.dumps(body)}\n{end}\n'.encode())
    # `result` reads the target's text output.
    return SimpleNamespace(stdout=f'{begin}\n{json.dumps(target[0])}\n{end}\n')
service = SpawnService(state=state)
with patch('plangraph.spawner.overflow.registry_caps', return_value={}), \
     patch('plangraph.spawner.overflow.placement_config', return_value=routes), \
     patch.object(remote_ps.subprocess, 'run', side_effect=ssh):
    out = io.StringIO()
    with patch('sys.stdin', io.StringIO('fix round')), contextlib.redirect_stdout(out):
        assert cli.main(['--state', str(state), 'resume', run_id, '--host', 'posix']) == 0
    assert json.loads(out.getvalue())['resume_number'] == 1
    saved = service.result(run_id)
    assert saved['status'] == 'running' and saved['resume_requested'] == 1, saved
    # The target still shows the previous round: the origin keeps the round running.
    seen = asyncio.run(service.refresh_result(run_id))
    assert seen['status'] == 'running' and seen['final_head'] == 'old-head', seen
    assert 'remote_query_error' not in seen, seen
    # The target reports the requested round: its outcome replaces the receipt.
    target[0] = dict(base, status='completed', ended=20.0, final_head='new-head', resume_number=1)
    done = asyncio.run(service.refresh_result(run_id))
    assert done['status'] == 'completed' and done['final_head'] == 'new-head', done
    assert 'resume_requested' not in service.result(run_id)
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

/// The acknowledgement of a routed fix round is lost (transport timeout) although the target
/// reopened the run: the origin reconciles the requested round from the target's record.
#[test]
fn lost_resume_acknowledgement_is_reconciled_from_the_target() {
    let state = tempfile::tempdir().unwrap();
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let output = test_python::command()
        .env("PYTHONPATH", source)
        .arg("-c")
        .arg(
            r#"
import contextlib, io, json, pathlib, re, subprocess, sys
from types import SimpleNamespace
from unittest.mock import patch
from plangraph.spawner import cli, remote_ps
from plangraph.spawner.service import SpawnService

state = pathlib.Path(sys.argv[1])
run_id = 'run-fixture2'
base = dict(run_id=run_id, run_kind='native-codex', role='implement', host='posix')
origin = dict(base, status='completed', ended=10.0, started=1.0, overflow_remote=True,
              origin_receipt=True, final_head='old-head', resume_number=0)
(state / 'runs' / run_id).mkdir(parents=True)
(state / 'runs' / run_id / 'record.json').write_text(json.dumps(origin))
host = dict(repo=str(state), ssh='fixture', spawner_src=str(state), state_dir=str(state))
routes = {'hosts': {'posix': host}}
reopened = dict(base, status='running', started=15.0, resume_number=1)
timeouts, sent = [], []
def ssh(argv, *, timeout, **kwargs):
    timeouts.append(timeout)
    begin, end = re.findall(r'PLANGRAPH_RECEIPT_[a-f0-9]+_(?:BEGIN|END)', argv[-1])
    if 'resume-local' in argv[-1]:
        sent.append(re.search(r'--request-id ([0-9a-f]{32})', argv[-1]).group(1))
        raise subprocess.TimeoutExpired(argv, timeout)
    # The target reopened the run for the request that was sent, or for another one.
    return SimpleNamespace(stdout=f'{begin}\n{json.dumps(dict(reopened, resume_request_id=answer[0] or sent[-1]))}\n{end}\n')
service = SpawnService(state=state)
answer = ['other-request']
with patch('plangraph.spawner.overflow.registry_caps', return_value={}), \
     patch('plangraph.spawner.overflow.placement_config', return_value=routes), \
     patch.object(remote_ps.subprocess, 'run', side_effect=ssh):
    # A newer round that carries another request's id never confirms this prompt.
    out = io.StringIO()
    with patch('sys.stdin', io.StringIO('fix round')), contextlib.redirect_stdout(out):
        cli.main(['--state', str(state), 'resume', run_id, '--host', 'posix'])
    reply = json.loads(out.getvalue())
    assert reply['ok'] is False and 'resume_requested' not in service.result(run_id), reply
    answer[0] = None
    out = io.StringIO()
    with patch('sys.stdin', io.StringIO('fix round')), contextlib.redirect_stdout(out):
        cli.main(['--state', str(state), 'resume', run_id, '--host', 'posix'])
    reply = json.loads(out.getvalue())
    assert reply['ok'] and reply['resume_number'] == 1 and reply['reconciled'], reply
    assert reply['request_id'] == sent[-1] and sent[0] != sent[1], (reply, sent)
    # The resume waits longer than the owner's reopen wait (30 s); `result` keeps the short bound.
    assert timeouts[0] == remote_ps.RESUME_TIMEOUT > 30 and timeouts[1] == 10, timeouts
    assert len(timeouts) == 4, timeouts
    saved = service.result(run_id)
    assert saved['status'] == 'running' and saved['resume_requested'] == 1, saved
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

/// The local owner path waits as long as the ssh path for a slow reopen.
#[test]
fn local_resume_waits_for_a_slow_reopen() {
    let state = tempfile::tempdir().unwrap();
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let output = test_python::command()
        .env("PYTHONPATH", source)
        .arg("-c")
        .arg(
            r#"
import contextlib, io, json, pathlib, re, subprocess, sys
from types import SimpleNamespace
from unittest.mock import patch
from plangraph.spawner import cli, remote_ps

state = pathlib.Path(sys.argv[1])
run_id = 'run-fixture3'
(state / 'runs' / run_id).mkdir(parents=True)
(state / 'runs' / run_id / 'record.json').write_text(json.dumps(dict(
    run_id=run_id, run_kind='native-codex', role='implement', host='local',
    status='completed', ended=1.0, started=0.5)))
waits, answer = [], [None]
def run(argv, *, timeout, **kwargs):
    # The owner takes 25 s to reopen: a shorter bound would lose the acknowledgement.
    waits.append(timeout)
    if timeout < 25:
        raise subprocess.TimeoutExpired(argv, timeout)
    sent = argv[argv.index('--request-id') + 1]
    echoed = answer[0] or sent
    return SimpleNamespace(returncode=0, stdout=json.dumps(dict(run_id=run_id, ok=True, resumed=True, request_id=echoed)).encode())
with patch('plangraph.fleet_snapshot._projection_binary', return_value='pg-spawnd'), \
     patch.object(remote_ps.subprocess, 'run', side_effect=run):
    out = io.StringIO()
    with patch('sys.stdin', io.StringIO('fix round')), contextlib.redirect_stdout(out):
        cli.main(['--state', str(state), 'resume', run_id])
    assert json.loads(out.getvalue())['resumed'] is True and waits == [remote_ps.RESUME_TIMEOUT], waits
    # An owner that confirms another request's id is not accepted.
    answer[0] = 'f' * 32
    out = io.StringIO()
    with patch('sys.stdin', io.StringIO('fix round')), contextlib.redirect_stdout(out):
        cli.main(['--state', str(state), 'resume', run_id])
    reply = json.loads(out.getvalue())
    assert reply['ok'] is False and 'does not confirm' in reply['error'], reply
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
