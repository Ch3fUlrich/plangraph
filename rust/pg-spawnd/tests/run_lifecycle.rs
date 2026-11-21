use serde_json::{json, Value};
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

fn command(state: &Path) -> Command {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"));
    cmd.env("PLANGRAPH_SPAWN_STATE", state);
    cmd
}

fn publish(state: &Path, status: &str, ended: Value) {
    let mut child = command(state)
        .arg("record-publish")
        .stdin(Stdio::piped())
        .spawn()
        .unwrap();
    write!(
        child.stdin.take().unwrap(),
        "{}",
        json!({"run_id":"run-fixture","host":"fixture","role":"review",
               "status":status,"started":1,"ended":ended,"exit_code":null,
               "stderr_tail":"private output"})
    )
    .unwrap();
    assert!(child.wait().unwrap().success());
}

fn page(state: &Path, cursor: &str) -> Value {
    let output = command(state)
        .args(["runs", "--changed-since", cursor])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn lifecycle_replays_terminal_publication_after_interruption() {
    let state = tempfile::tempdir().unwrap();
    publish(state.path(), "queued", Value::Null);
    let first = page(state.path(), "0");
    assert_eq!(first["rows"][0]["phase"], "queued");

    publish(state.path(), "running", Value::Null);
    let record = state.path().join("runs/run-fixture/record.json");
    let running = std::fs::read(&record).unwrap();
    publish(state.path(), "failed", json!(2));
    publish(state.path(), "failed", json!(2));

    // The event committed, but the process stopped before updating the projection.
    std::fs::write(&record, running).unwrap();
    std::fs::write(state.path().join("run-lifecycle/published"), b"2").unwrap();
    let resumed = page(state.path(), "1");
    let rows = resumed["rows"].as_array().unwrap();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0]["phase"], "started");
    assert_eq!(rows[1]["phase"], "ended");
    assert_eq!(rows[1]["status"], "failed");
    assert!(rows[1].get("stderr_tail").is_none());
    assert_eq!(resumed["next_cursor"], 3);
    let saved: Value = serde_json::from_slice(&std::fs::read(record).unwrap()).unwrap();
    assert_eq!(saved["status"], "failed");
}

#[test]
fn legacy_spawner_publishes_and_replays_backlog_after_host_recovers() {
    if !cfg!(unix) {
        return;
    }
    let state = tempfile::tempdir().unwrap();
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let bin = Path::new(env!("CARGO_BIN_EXE_pg-spawnd"));
    let path = std::env::join_paths(std::iter::once(bin.parent().unwrap().to_owned()).chain(
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()),
    ))
    .unwrap();
    let output = Command::new(if cfg!(windows) { "python" } else { "python3" })
        .env("PYTHONPATH", repo.join("src"))
        .env("PATH", path)
        .arg("-c")
        .arg(
            r#"
import json, sys
from unittest.mock import patch
from plangraph.spawner.remote_ps import changed_runs
from plangraph.spawner.service import SpawnService

state, binary = sys.argv[1:]
with patch('plangraph.fleet_snapshot._projection_binary', return_value=binary):
    service = SpawnService(state=state)
    run = {'run_id':'run-legacy','status':'running','started':1,'role':'review'}
    service._save(run)
    service._save(dict(run, status='failed', ended=2, exit_code=7))
    def offline(host, timeout):
        assert host['lifecycle_cursor'] == 0
        raise TimeoutError()
    def online(host, timeout):
        assert host['lifecycle_cursor'] == 0
        return json.dumps({'rows':[{'seq':1,'phase':'ended','status':'failed'}],
                           'next_cursor':1,'more':False})
    with patch('plangraph.spawner.remote_ps.configured_hosts', return_value=[{'name':'fixture'}]):
        first = changed_runs(state, {}, transport=offline)
        assert [r['phase'] for r in first['local']['rows']] == ['started','ended']
        assert first['hosts']['fixture']['error']
        second = changed_runs(state, first['next_cursor'], transport=online)
        assert second['local']['rows'] == []
        assert second['hosts']['fixture']['rows'][0]['status'] == 'failed'
        assert second['next_cursor']['hosts']['fixture'] == 1
        from plangraph.spawner.stall_probe import probe_run
        orphan = {'run_id':'run-orphan','status':'running','started':1,'role':'review','pid':123}
        service._save(orphan)
        with patch('plangraph.spawner.stall_probe.os.kill', side_effect=ProcessLookupError()):
            assert probe_run(state, orphan, 3)['rule'] == 'run-orphan'
        third = changed_runs(state, second['next_cursor'], transport=lambda *_: '{"rows":[],"next_cursor":1,"more":false}')
        assert [r['status'] for r in third['local']['rows']] == ['running','failed']

"#,
        )
        .arg(state.path())
        .arg(bin)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
