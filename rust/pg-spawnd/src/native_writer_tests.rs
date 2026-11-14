use super::*;
use serde_json::json;

#[derive(Default)]
struct Fake {
    calls: Vec<Option<String>>,
    heads: usize,
    review_after: usize,
    stale: bool,
    disk: u64,
    disk_after_prepare: Option<u64>,
    gates: bool,
}
impl Driver for Fake {
    fn prepare(&mut self) -> Result<String, String> {
        if self.stale {
            Err("stale base".into())
        } else {
            if let Some(disk) = self.disk_after_prepare {
                self.disk = disk;
            }
            Ok("base".into())
        }
    }
    fn disk_gb(&mut self) -> Result<u64, String> {
        Ok(self.disk)
    }
    fn execute(&mut self, session: Option<&str>, _: &str) -> Result<String, String> {
        self.calls.push(session.map(str::to_owned));
        Ok("same-session".into())
    }
    fn head(&mut self) -> Result<String, String> {
        self.heads += 1;
        Ok("final-head".into())
    }
    fn review(&mut self, _: &str) -> Result<Option<serde_json::Value>, String> {
        Ok((self.calls.len() > self.review_after).then(|| json!({"verdict":"ACCEPT"})))
    }
    fn gates(&mut self) -> Result<(), String> {
        if self.gates {
            Ok(())
        } else {
            Err("gate failed".into())
        }
    }
    fn save(&mut self, _: &serde_json::Value) -> Result<(), String> {
        Ok(())
    }
}
fn fake() -> Fake {
    Fake {
        disk: 5,
        gates: true,
        ..Fake::default()
    }
}
#[test]
fn native_missing_final_review_resumes_same_session_and_records_host_head() {
    let mut driver = Fake {
        review_after: 1,
        ..fake()
    };
    let record = run(&mut driver, json!({"host":"other-host"}), "brief");
    assert_eq!(driver.calls, vec![None, Some("same-session".into())]);
    assert_eq!(record["host"], "other-host");
    assert_eq!(record["base_sha"], "base");
    assert_eq!(record["final_head"], "final-head");
    assert_eq!(record["exit_code"], 0);
}
#[test]
fn native_disk_floor_and_stale_base_prevent_execution() {
    for (mut driver, code) in [
        (Fake { disk: 4, ..fake() }, 8),
        (
            Fake {
                disk_after_prepare: Some(4),
                ..fake()
            },
            8,
        ),
        (
            Fake {
                stale: true,
                ..fake()
            },
            5,
        ),
    ] {
        let record = run(&mut driver, json!({}), "brief");
        assert_eq!(record["exit_code"], code);
        assert!(driver.calls.is_empty());
    }
}
#[test]
fn native_resume_cap_and_final_gates_fail_closed() {
    let mut driver = Fake {
        review_after: 9,
        ..fake()
    };
    assert_eq!(run(&mut driver, json!({}), "brief")["exit_code"], 4);
    assert_eq!(driver.calls.len(), 4);
    let mut driver = Fake {
        gates: false,
        ..fake()
    };
    assert_eq!(run(&mut driver, json!({}), "brief")["exit_code"], 7);
}
#[test]
fn native_placement_uses_existing_ram_overflow_and_disk_floor() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new("python3").current_dir(&root).env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
from unittest.mock import patch
from types import SimpleNamespace
from plangraph.spawner import seat_spawn as s, overflow as o
row = dict(ssh='other', ok=True, fresh=True, live_runs=0, mem_available_mb=17000, role_mb=2048, floor_mb=3000, disk_free_gb=6)
opts = dict(card={}, prompt='brief')
card = dict(run_kind='native-codex', base_ref='refs/heads/main')
with patch.object(o, 'registry_caps', return_value={'self':'primary','hosts':[]}), patch.object(o, 'local_limits', return_value=(2048,3000)), patch.object(s, 'resources', return_value=(0,1000)), patch.object(o, 'ready_records', return_value={'other':row}), patch.object(o, 'repos_public', return_value=True):
    placed = s.admit(SimpleNamespace(runner=SimpleNamespace()), opts, card=card, preview=True)
    assert placed['host'] == 'other', placed
    row['disk_free_gb'] = 4
    refused = s.admit(SimpleNamespace(runner=SimpleNamespace()), opts, card=card, preview=True)
    assert refused['host'] != 'other', refused
    row.update(disk_free_gb=6)
    row.pop('refusal', None)
    card['repo'] = '/nonexistent-controller-repo'
    with patch.object(o, 'placement_policy', return_value=('primary',True)), patch('shutil.disk_usage', side_effect=FileNotFoundError):
        placed = s.admit(SimpleNamespace(runner=SimpleNamespace()), opts, card=card, preview=True)
        assert placed['host'] == 'other', placed
        row['refusal'] = 'stale readiness'
        row.pop('disk_free_gb')
        refused = s.admit(SimpleNamespace(runner=SimpleNamespace()), opts, card=card, preview=True)
        assert row['refusal'] == 'stale readiness', row
        assert refused['host'] != 'other' and refused['reason'] == 'disk_low: measurement or configuration unavailable', refused
"#]).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
#[test]
fn native_remote_result_returns_final_head_and_review_to_origin_record() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new("python3").current_dir(&root).env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
import json, os, pathlib, tempfile
from unittest.mock import patch
from types import SimpleNamespace
from plangraph.spawner.remote_ps import native_result
with tempfile.TemporaryDirectory() as directory:
    config = pathlib.Path(directory)/'hosts.json'
    config.write_text(json.dumps({'hosts':{'other':{'repo':'/repo','spawner_src':'/src','ssh':'fixture','env_file':'/env'}}}))
    receipt = dict(run_id='run-abc', run_kind='native-codex', host='other', status='running', overflow_remote=True)
    final = dict(receipt, status='completed', final_head='head', base_sha='base', exit_code=0, review={'verdict':'ACCEPT','text':'VERDICT: ACCEPT'})
    with patch.dict(os.environ, PLANGRAPH_OVERFLOW_HOSTS=str(config)), patch('plangraph.spawner.remote_ps.secrets.token_hex', return_value='fixture'), patch('plangraph.spawner.remote_ps.subprocess.run', return_value=SimpleNamespace(stdout='shell banner\nPLANGRAPH_RECEIPT_fixture_BEGIN\n' + json.dumps(final) + '\nPLANGRAPH_RECEIPT_fixture_END\ntrailer', returncode=0)):
        result = native_result(receipt)
    assert result['final_head'] == 'head' and result['review']['verdict'] == 'ACCEPT', result
    assert result['host'] == 'other' and result['exit_code'] == 0, result
"#]).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn native_rust_mcp_bridge_rejects_review_before_starting_adapter() {
    let args = json!({"card":{"run_kind":"native-codex"},"role":"review","prompt":"brief"});
    let result =
        crate::placement::native_spawn(std::path::Path::new("/unused"), args.as_object().unwrap());
    assert!(result.is_err());
}
#[test]
fn native_remote_receipts_never_probe_or_kill_origin_pids_and_cancel_on_target() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new("python3").current_dir(&root).env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
import asyncio, json, os, pathlib, tempfile, time
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
from plangraph.spawner.remote_ps import native_result
from plangraph.spawner.stall_probe import probe_run
from plangraph.spawner.cancel_recorded import cancel_recorded
from plangraph.spawner.service import SpawnService
with tempfile.TemporaryDirectory() as directory:
    config = pathlib.Path(directory)/'hosts.json'
    config.write_text(json.dumps({'hosts':{'other':{'repo':'/repo','spawner_src':'/src','ssh':'fixture','env_file':'/env'}}}))
    receipt = dict(run_id='run-abc', run_kind='native-codex', host='other', status='running', overflow_remote=True, started=time.time(), pid=os.getpid(), pid_start=99, supervisor_pid_start=88, supervisor_stderr_path='/remote/log')
    with patch.dict(os.environ, PLANGRAPH_OVERFLOW_HOSTS=str(config)), patch('plangraph.spawner.remote_ps.secrets.token_hex', return_value='fixture'), patch('plangraph.spawner.remote_ps.subprocess.run', return_value=SimpleNamespace(stdout='shell banner\nPLANGRAPH_RECEIPT_fixture_BEGIN\n' + json.dumps(receipt) + '\nPLANGRAPH_RECEIPT_fixture_END\ntrailer', returncode=0)):
        running = native_result(receipt)
    assert 'pid' not in running and 'pid_start' not in running and not any(k.startswith('supervisor_') for k in running), running
    persisted = dict(receipt)
    actual = SpawnService(state=pathlib.Path(directory)/'state')
    actual._save(persisted)
    stored = json.loads(actual._path(receipt['run_id']).read_text())
    assert 'pid' not in stored and not any(k.startswith('supervisor_') for k in stored), stored
    with patch('plangraph.spawner.stall_probe.os.kill', side_effect=ProcessLookupError) as kill:
        probe_run(directory, receipt, time.time())
        kill.assert_not_called()
    cancelled = dict(receipt, status='cancelled', exit_code=1)
    runner = SimpleNamespace(jobs={}, kill_recorded=AsyncMock(side_effect=AssertionError('remote pid reached local killer')))
    saved = []
    service = SimpleNamespace(result=lambda *a, **kw: receipt, runner=runner, tasks={}, _save=saved.append)
    with patch.dict(os.environ, PLANGRAPH_OVERFLOW_HOSTS=str(config)), patch('plangraph.spawner.remote_ps.secrets.token_hex', return_value='fixture'), patch('plangraph.spawner.remote_ps.subprocess.run', return_value=SimpleNamespace(stdout='shell banner\nPLANGRAPH_RECEIPT_fixture_BEGIN\n' + json.dumps(cancelled) + '\nPLANGRAPH_RECEIPT_fixture_END\ntrailer', returncode=1)) as ssh:
        result = asyncio.run(cancel_recorded(service, receipt['run_id']))
        assert 'plangraph.spawner.cli cancel run-abc' in ssh.call_args.args[0][-1], ssh.call_args
        assert '. /env >/dev/null 2>&1' in ssh.call_args.args[0][-1], ssh.call_args
    runner.kill_recorded.assert_not_called()
    assert result['status'] == 'cancelled' and 'pid' not in result and saved[-1] == result, result
    with patch.dict(os.environ, PLANGRAPH_OVERFLOW_HOSTS=str(config)), patch('plangraph.spawner.remote_ps.secrets.token_hex', return_value='fixture'), patch('plangraph.spawner.remote_ps.subprocess.run', side_effect=TimeoutError):
        result = asyncio.run(cancel_recorded(service, receipt['run_id']))
    assert result['status'] == 'running' and result['remote_query_error'] == 'host query failed', result
"#]).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn native_arguments_are_validated_before_adapter_launch() {
    for (key, value) in [
        ("dry_run", json!(true)),
        ("dry_run", json!("yes")),
        ("cwd", json!("/repo")),
        ("seats", json!(true)),
        ("model_or_combo", json!("model")),
        ("override_reason", json!("reason")),
        ("review_scope", json!("diff")),
        ("run_worktree", json!(true)),
        ("wait_seconds", json!(-1)),
    ] {
        let mut args = json!({"card":{"run_kind":"native-codex","repo":"/repo"},"prompt":"brief"});
        args[key] = value;
        assert!(
            matches!(
                crate::placement::native_spawn(
                    std::path::Path::new("/unused"),
                    args.as_object().unwrap()
                ),
                Err(crate::errors::ToolError::Refused { .. })
            ),
            "{key}"
        );
    }
    for card in [
        json!({"run_kind":"native-codex","cwd":"/repo"}),
        json!({"run_kind":"native-codex","findings":[{"text":"fix"}]}),
    ] {
        let args = json!({"card":card,"prompt":"brief"});
        assert!(matches!(
            crate::placement::native_spawn(
                std::path::Path::new("/unused"),
                args.as_object().unwrap()
            ),
            Err(crate::errors::ToolError::Refused { .. })
        ));
    }
}

#[test]
fn native_cards_require_explicit_absolute_repo_before_admission() {
    for repo in [Value::Null, json!(""), json!("relative/repo"), json!(42)] {
        let mut card = json!({"run_kind":"native-codex", "base_ref":"refs/heads/main"});
        if !repo.is_null() {
            card["repo"] = repo;
        }
        let args = json!({"card":card,"prompt":"brief"});
        match crate::placement::native_spawn(
            std::path::Path::new("/unused"),
            args.as_object().unwrap(),
        ) {
            Err(crate::errors::ToolError::Refused { message, .. }) => {
                assert_eq!(message, "native-codex requires an absolute card.repo");
            }
            _ => panic!("invalid repo must be refused before adapter launch"),
        }
    }
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new("python3").current_dir(&root).env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
import asyncio, pathlib, tempfile
from unittest.mock import patch
from plangraph.spawner.service import SpawnService
from plangraph.spawner.overflow import native_spawn
async def check():
    with tempfile.TemporaryDirectory() as directory:
        service = SpawnService(state=pathlib.Path(directory)/'state')
        for repo in (None, '', 'relative/repo', 42):
            card = dict(run_kind='native-codex', base_ref='refs/heads/main')
            if repo is not None: card['repo'] = repo
            with patch.object(service, 'admit_spawn', side_effect=AssertionError('admission reached')), patch('asyncio.create_subprocess_exec', side_effect=AssertionError('worker launched')):
                for operation in (service.spawn(card=card, prompt='brief'), native_spawn(service, card, dict(prompt='brief'))):
                    try: await operation
                    except ValueError as exc: assert str(exc) == 'native-codex requires an absolute card.repo', exc
                    else: raise AssertionError('invalid repo accepted')
asyncio.run(check())
"#]).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn native_remote_reads_are_local_and_explicit_refresh_does_not_block_loop() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new("python3").current_dir(&root).env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
import asyncio, pathlib, tempfile, threading
from unittest.mock import patch
from plangraph.spawner.service import SpawnService
from plangraph.spawner.mcp import MCPSpawnerServer
async def check():
    with tempfile.TemporaryDirectory() as directory:
        service = SpawnService(state=pathlib.Path(directory)/'state')
        receipt = dict(run_id='run-abc', run_kind='native-codex', host='other', status='running', overflow_remote=True)
        service._save(receipt)
        with patch('plangraph.spawner.remote_ps.native_result', side_effect=AssertionError('local read contacted SSH')):
            assert service.result('run-abc')['status'] == 'running'
        server = MCPSpawnerServer(state=service.state)
        server.service = service
        for verb in ('result', 'status'):
            entered, release = threading.Event(), threading.Event()
            loop_thread = threading.get_ident()
            def refresh(record):
                assert threading.get_ident() != loop_thread, 'SSH on event loop'
                entered.set()
                assert release.wait(2), 'event loop did not progress while SSH waited'
                return dict(record, final_head='refreshed-head')
            with patch('plangraph.spawner.remote_ps.native_result', side_effect=refresh) as ssh:
                task = asyncio.create_task(server.call_tool('plangraph_agent_' + verb, {'run_id':'run-abc'}))
                try:
                    assert await asyncio.to_thread(entered.wait, 2), 'refresh never started'
                    await asyncio.sleep(0)
                    assert not task.done(), 'poll did not wait for refresh'
                finally: release.set()
                assert (await task)['final_head'] == 'refreshed-head'
                assert ssh.call_count == 1
        with patch('plangraph.spawner.remote_ps.native_result', return_value=dict(receipt, status='cancelled')) as ssh:
            assert (await service.cancel('run-abc'))['status'] == 'cancelled'
            assert ssh.call_count == 1, 'cancel must perform only its cancel round trip'
            assert ssh.call_args.kwargs == {'verb':'cancel'}
asyncio.run(check())
"#]).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
