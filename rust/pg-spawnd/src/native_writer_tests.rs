use super::*;
use serde_json::json;

#[derive(Default)]
struct Fake {
    calls: Vec<Option<String>>,
    cancel_requested: bool,
    saved: Value,
    heads: usize,
    stale: bool,
    disk: u64,
    disk_after_prepare: Option<u64>,
    gates: bool,
    failures: usize,
    refreshes: usize,
    refresh_error: bool,
    stop_on_refresh: bool,
    prompts: Vec<String>,
    skill: Option<String>,
    /// Verdict and HIGH count of each review run, in order; then ACCEPT.
    verdicts: Vec<(&'static str, usize)>,
    /// Head a review run reports instead of the requested one.
    review_head: Option<&'static str>,
    /// Fast-check runs that fail before the first green one.
    fast_failures: usize,
    stop_on_fast_checks: bool,
    /// HEAD seen at each call count (indexed by calls so far, the last entry repeats); overrides the default.
    head_script: Vec<&'static str>,
    /// Turns after which HEAD is still the base (no commit yet).
    uncommitted_turns: usize,
    /// When false, green fast checks leave no stamp.
    stamps: bool,
    stamped: Vec<String>,
    reviews: Vec<Option<serde_json::Value>>,
    defer_review_on: Option<usize>,
    fail_first_review: bool,
    pushed: Vec<String>,
    push_failures: usize,
}
impl Driver for Fake {
    fn cancelled(&self) -> bool {
        self.cancel_requested
    }
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
    fn refresh_base(&mut self) -> Result<String, String> {
        self.refreshes += 1;
        if self.stop_on_refresh {
            return Err(crate::writer_control::STOPPED.into());
        }
        if self.refresh_error {
            return Err("merge conflict".into());
        }
        Ok("fresh-base".into())
    }
    fn disk_gb(&mut self) -> Result<u64, String> {
        Ok(self.disk)
    }
    fn execute(&mut self, session: Option<&str>, prompt: &str) -> Result<String, String> {
        self.calls.push(session.map(str::to_owned));
        self.prompts.push(prompt.into());
        if self.calls.len() <= self.failures {
            return Err("codex killed: SIGKILL".into());
        }
        Ok("same-session".into())
    }
    fn head(&mut self) -> Result<String, String> {
        self.heads += 1;
        if let Some(head) = self
            .head_script
            .get(self.calls.len())
            .or(self.head_script.last())
        {
            return Ok((*head).into());
        }
        // Every writer turn after the uncommitted ones commits: the head changes per turn.
        Ok(
            if self.calls.is_empty() || self.calls.len() <= self.uncommitted_turns {
                "base".into()
            } else if self.calls.len() <= self.uncommitted_turns + 1 {
                "final-head".into()
            } else {
                format!("final-head-{}", self.calls.len())
            },
        )
    }
    fn fast_checks(&mut self, head: &str) -> Result<(), String> {
        if self.fast_failures > 0 {
            self.fast_failures -= 1;
            return Err("check_file_size.py: too large".into());
        }
        if self.stop_on_fast_checks {
            return Err(crate::writer_control::STOPPED.into());
        }
        if self.stamps {
            self.stamped.push(head.into());
        }
        Ok(())
    }
    fn stamped(&mut self, head: &str) -> bool {
        self.stamped.iter().any(|h| h == head)
    }
    fn review(
        &mut self,
        head: &str,
        previous: Option<&serde_json::Value>,
    ) -> Result<serde_json::Value, String> {
        self.reviews.push(previous.cloned());
        if self.fail_first_review && self.reviews.len() == 1 {
            return Err("temporary route preview failure".into());
        }
        if self
            .defer_review_on
            .is_some_and(|call| self.reviews.len() >= call)
        {
            return Ok(json!({"status":"deferred_to_batch_seat","head":head,
                "causes":[{"leg":"mimo-v2.6-pro","reason":"repository_review_tools_unavailable"},
                          {"leg":"agy-gemini-4-hydrogen-high","reason":"limited"}]}));
        }
        let (verdict, high) = if self.verdicts.is_empty() {
            ("ACCEPT", 0)
        } else {
            self.verdicts.remove(0)
        };
        let text = format!(
            "{}VERDICT: {verdict}\n",
            "HIGH a.rs:1 - finding\n".repeat(high)
        );
        Ok(
            json!({"verdict":verdict, "head":self.review_head.unwrap_or(head),
            "run_id":format!("run-review{}", self.reviews.len()), "text":text}),
        )
    }
    fn gates(&mut self) -> Result<(), String> {
        if self.gates {
            Ok(())
        } else {
            Err("gate failed".into())
        }
    }
    fn push(&mut self, head: &str) -> Result<Option<String>, String> {
        self.pushed.push(head.into());
        if self.pushed.len() <= self.push_failures {
            return Err("pre-push: fixture scrub hit".into());
        }
        Ok(Some("refs/heads/lane/fixture".into()))
    }
    fn save(&mut self, record: &Value) -> Result<(), String> {
        self.saved = record.clone();
        Ok(())
    }
    fn skill(&mut self, _: &str) -> Result<String, String> {
        self.skill.clone().ok_or_else(|| "skill unavailable".into())
    }
}
fn fake() -> Fake {
    Fake {
        disk: 5,
        gates: true,
        stamps: true,
        ..Fake::default()
    }
}
#[test]
fn cancelled_writer_publishes_exit_130_without_starting_a_turn() {
    let mut driver = Fake {
        cancel_requested: true,
        disk: 10,
        ..Fake::default()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["status"], "cancelled");
    assert_eq!(record["exit_code"], 130);
    assert_eq!(record["reason"], "cancelled");
    assert_eq!(driver.saved, record);
    assert!(driver.calls.is_empty());
}

#[test]
fn native_missing_final_review_resumes_same_session_and_records_host_head() {
    // REQUEST_CHANGES resumes the same session; the second review on the head accepts.
    let mut driver = Fake {
        verdicts: vec![("REQUEST_CHANGES", 1)],
        ..fake()
    };
    let record = run(&mut driver, json!({"host":"other-host"}), "brief");
    assert_eq!(driver.calls, vec![None, Some("same-session".into())]);
    assert!(driver.prompts[1].contains("HIGH a.rs:1 - finding"));
    assert_eq!(driver.reviews[1].as_ref().unwrap()["run_id"], "run-review1");
    assert_eq!(record["host"], "other-host");
    assert_eq!(record["base_sha"], "base");
    assert_eq!(record["final_head"], "final-head-2");
    assert_eq!(record["review_rounds"], 2);
    assert_eq!(record["round_counts"], json!([1]));
    assert_eq!(record["reviews"][0]["verdict"], "REQUEST_CHANGES");
    assert_eq!(record["review"]["run_id"], "run-review2");
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
    // Falling HIGH+MED counts at or below half of round 1 from round 3 on keep progress;
    // the eighth REQUEST_CHANGES round is the ceiling of the one stop rule (round_progress.rs).
    let mut driver = Fake {
        verdicts: vec![
            ("REQUEST_CHANGES", 20),
            ("REQUEST_CHANGES", 15),
            ("REQUEST_CHANGES", 9),
            ("REQUEST_CHANGES", 8),
            ("REQUEST_CHANGES", 7),
            ("REQUEST_CHANGES", 6),
            ("REQUEST_CHANGES", 5),
            ("REQUEST_CHANGES", 4),
        ],
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 3);
    assert_eq!(record["round_stop"], "round_cap");
    assert_eq!(record["review_rounds"], 8);
    assert_eq!(driver.calls.len(), 8);
    assert_eq!(driver.reviews.len(), 8);
    assert!(driver.pushed.is_empty());
    let mut driver = Fake {
        gates: false,
        ..fake()
    };
    assert_eq!(run(&mut driver, json!({}), "brief")["exit_code"], 7);
    assert!(driver.pushed.is_empty());
}
#[test]
fn native_placement_uses_existing_ram_overflow_and_disk_floor() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new(if cfg!(windows) { "python" } else { "python3" }).current_dir(&root).env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
from unittest.mock import patch
from types import SimpleNamespace
from plangraph.spawner import seat_spawn as s, overflow as o, host_ready as h
row = dict(ssh='other', ok=True, fresh=True, live_runs=0, mem_available_mb=17000, role_mb=2048, floor_mb=3000, disk_free_gb=6)
opts = dict(card={}, prompt='brief')
card = dict(run_kind='native-codex', base_ref='refs/heads/main')
with patch.object(o, 'registry_caps', return_value={'self':'primary','hosts':[]}), patch.object(o, 'local_limits', return_value=(2048,3000)), patch.object(s, 'resources', return_value=(0,1000)), patch.object(o, 'ready_records', return_value={'other':row}), patch.object(o, 'repos_public', return_value=True), patch.object(h, 'cargo_target_size_gb', return_value=0.0):
    placed = s.admit(SimpleNamespace(runner=SimpleNamespace(), ps=lambda: {'runs': []}), opts, card=card, preview=True)
    assert placed['host'] == 'other', placed
    row['disk_free_gb'] = 4
    refused = s.admit(SimpleNamespace(runner=SimpleNamespace(), ps=lambda: {'runs': []}), opts, card=card, preview=True)
    assert refused['host'] != 'other', refused
    row.update(disk_free_gb=6)
    row.pop('refusal', None)
    card['repo'] = '/nonexistent-controller-repo'
    with patch.object(o, 'placement_policy', return_value=('primary',True)), patch('shutil.disk_usage', side_effect=FileNotFoundError):
        placed = s.admit(SimpleNamespace(runner=SimpleNamespace(), ps=lambda: {'runs': []}), opts, card=card, preview=True)
        assert placed['host'] == 'other', placed
        row['refusal'] = 'stale readiness'
        row.pop('disk_free_gb')
        refused = s.admit(SimpleNamespace(runner=SimpleNamespace(), ps=lambda: {'runs': []}), opts, card=card, preview=True)
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
    let output = std::process::Command::new(if cfg!(windows) { "python" } else { "python3" }).current_dir(&root).env("PYTHONPATH", root.join("src"))
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
    with patch('plangraph.spawner.overflow.placement_config', return_value=json.loads(config.read_text())), patch('plangraph.spawner.remote_ps.secrets.token_hex', return_value='fixture'), patch('plangraph.spawner.remote_ps.subprocess.run', return_value=SimpleNamespace(stdout='shell banner\nPLANGRAPH_RECEIPT_fixture_BEGIN\n' + json.dumps(final) + '\nPLANGRAPH_RECEIPT_fixture_END\ntrailer', returncode=0)):
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
    let args =
        json!({"card":{"run_kind":"native-codex","repo":"/repo"},"role":"review","prompt":"brief"});
    let result =
        crate::placement::native_spawn(std::path::Path::new("/unused"), args.as_object().unwrap());
    assert!(result.is_err());
}
#[test]
fn native_remote_receipts_never_probe_or_kill_origin_pids_and_cancel_on_target() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new(if cfg!(windows) { "python" } else { "python3" }).current_dir(&root).env("PYTHONPATH", root.join("src"))
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
    with patch('plangraph.spawner.overflow.placement_config', return_value=json.loads(config.read_text())), patch('plangraph.spawner.remote_ps.secrets.token_hex', return_value='fixture'), patch('plangraph.spawner.remote_ps.subprocess.run', return_value=SimpleNamespace(stdout='shell banner\nPLANGRAPH_RECEIPT_fixture_BEGIN\n' + json.dumps(receipt) + '\nPLANGRAPH_RECEIPT_fixture_END\ntrailer', returncode=0)):
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
    with patch('plangraph.spawner.overflow.placement_config', return_value=json.loads(config.read_text())), patch('plangraph.spawner.remote_ps.secrets.token_hex', return_value='fixture'), patch('plangraph.spawner.remote_ps.subprocess.run', return_value=SimpleNamespace(stdout='shell banner\nPLANGRAPH_RECEIPT_fixture_BEGIN\n' + json.dumps(cancelled) + '\nPLANGRAPH_RECEIPT_fixture_END\ntrailer', returncode=1)) as ssh:
        result = asyncio.run(cancel_recorded(service, receipt['run_id']))
        assert 'plangraph.spawner.cli cancel run-abc' in ssh.call_args.args[0][-1], ssh.call_args
        assert '. /env >/dev/null 2>&1' in ssh.call_args.args[0][-1], ssh.call_args
    runner.kill_recorded.assert_not_called()
    assert result['status'] == 'cancelled' and 'pid' not in result and saved[-1] == result, result
    with patch('plangraph.spawner.overflow.placement_config', return_value=json.loads(config.read_text())), patch('plangraph.spawner.remote_ps.secrets.token_hex', return_value='fixture'), patch('plangraph.spawner.remote_ps.subprocess.run', side_effect=TimeoutError):
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
        json!({"run_kind":"native-codex","repo":"/repo","cwd":"/repo"}),
        json!({"run_kind":"native-codex","repo":"/repo","findings":[{"text":"fix"}]}),
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
    let output = std::process::Command::new(if cfg!(windows) { "python" } else { "python3" }).current_dir(&root).env("PYTHONPATH", root.join("src"))
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
    let output = std::process::Command::new(if cfg!(windows) { "python" } else { "python3" }).current_dir(&root).env("PYTHONPATH", root.join("src"))
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

#[test]
fn native_remote_wait_refreshes_terminal_evidence_at_a_bounded_cadence() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new(if cfg!(windows) { "python" } else { "python3" }).current_dir(&root).env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
import asyncio, pathlib, tempfile, time
from unittest.mock import patch
from plangraph.spawner.runner import spawn_service
async def check():
    with tempfile.TemporaryDirectory() as directory:
        service = spawn_service(state=pathlib.Path(directory)/'state')
        receipt = dict(run_id='run-abc', run_kind='native-codex', host='other', status='running', overflow_remote=True)
        final = dict(receipt, status='completed', ended=time.time(), exit_code=0, final_head='remote-head', review={'verdict':'ACCEPT'})
        service._save(receipt)
        with patch('plangraph.spawner.remote_ps.native_result', return_value=final) as ssh:
            result = await service.wait('run-abc', timeout=.5)
            assert result['final_head'] == 'remote-head' and result['review']['verdict'] == 'ACCEPT', result
            assert ssh.call_count == 1
        service._save(receipt)
        calls = []
        def refresh(record):
            calls.append(time.monotonic())
            return record if len(calls) == 1 else final
        with patch('plangraph.spawner.remote_ps.native_result', side_effect=refresh):
            assert (await service.wait('run-abc', timeout=3))['status'] == 'completed'
        assert len(calls) == 2 and calls[1] - calls[0] >= 1, calls
        service._save(receipt)
        with patch('plangraph.spawner.remote_ps.native_result', return_value=receipt) as ssh:
            try: await service.wait('run-abc', timeout=.1)
            except TimeoutError as exc: assert 'run wait timed out' in str(exc)
            else: raise AssertionError('running receipt did not time out')
            assert ssh.call_count == 1
asyncio.run(check())
"#]).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn native_killed_writer_relaunches_once_from_manifest() {
    let mut driver = Fake {
        failures: 1,
        ..fake()
    };
    let record = run(
        &mut driver,
        json!({"host":"fixture", "run_id":"run-fixture"}),
        "same brief",
    );
    assert_eq!(record["exit_code"], 0);
    assert_eq!(driver.calls, vec![None, None]);
    assert_eq!(driver.prompts[0], driver.prompts[1]);
    assert_eq!(driver.refreshes, 1);
    assert_eq!(record["base_sha"], "fresh-base");
    assert_eq!(record["manifest"]["brief"], "same brief");
    assert_eq!(record["manifest"]["try_count"], 2);
}
#[test]
fn native_third_failure_is_blocked_and_review_rejection_is_terminal() {
    let mut driver = Fake {
        failures: 3,
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(driver.calls.len(), 3);
    assert_eq!(record["result"], "BLOCKED");
    assert_eq!(record["manifest"]["try_count"], 3);
    // A rejection is terminal once the stop rule ends the rounds: equal counts twice.
    let mut driver = Fake {
        verdicts: vec![("REQUEST_CHANGES", 2); 3],
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 3);
    assert_eq!(record["round_stop"], "no_progress");
    assert_eq!(record["review"]["verdict"], "REQUEST_CHANGES");
    assert_eq!(driver.calls.len(), 3);
}

#[test]
fn native_relaunch_merge_failure_blocks_without_discarding_work() {
    let mut driver = Fake {
        failures: 1,
        refresh_error: true,
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["result"], "BLOCKED");
    assert_eq!(record["reason"], "merge conflict");
    assert_eq!(driver.calls.len(), 1);
}
#[test]
fn native_prompt_starts_with_header_then_role_skill() {
    let skill = "---\nname: implement\n---\nskill body\n";
    let mut driver = Fake {
        skill: Some(skill.into()),
        ..fake()
    };
    run(&mut driver, json!({}), "the brief");
    let header = crate::role_header::header("codex", "implement");
    assert!(driver.prompts[0].starts_with(&format!("{header}\n\n{skill}\nthe brief")));
    assert!(!driver.prompts[0].contains("AutoOS"));
}
#[test]
fn batch_driver_reaches_done_on_accept_and_pushes_once() {
    let mut driver = fake();
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 0);
    assert_eq!(record["status_line"], "DONE");
    assert_eq!(record["review_rounds"], 1);
    assert!(record["writer_turns"][0]["started"].is_string());
    assert!(record["writer_turns"][0]["ended"].is_string());
    assert_eq!(record["fast_checks"]["status"], "passed");
    assert_eq!(record["pushed"], "refs/heads/lane/fixture");
    assert_eq!(driver.pushed, vec!["final-head".to_owned()]);
    assert!(driver.reviews[0].is_none());
    assert!(!driver.prompts[0].contains("configured review directory"));
}
#[path = "native_writer_deferral_tests.rs"]
mod deferral;

#[test]
fn batch_driver_refuses_review_without_fast_checks_stamp() {
    let mut driver = Fake {
        stamps: false,
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert!(driver.reviews.is_empty());
    assert_eq!(record["exit_code"], 7);
    assert!(record["reason"]
        .as_str()
        .unwrap()
        .starts_with("review_refused: no fast-checks stamp"));
}
#[test]
fn batch_driver_resumes_on_fast_check_failure_then_fails() {
    let mut driver = Fake {
        fast_failures: 1,
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 0);
    assert_eq!(driver.calls, vec![None, Some("same-session".into())]);
    assert!(driver.prompts[1].contains("check_file_size.py: too large"));
    let mut driver = Fake {
        fast_failures: 9,
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 7);
    assert!(driver.reviews.is_empty());
    assert_eq!(driver.calls.len(), 1 + FAST_CHECK_FIXES);
}

#[path = "native_writer_stop_tests.rs"]
mod stop;

#[test]
fn batch_driver_retries_weak_or_other_head_reviews_then_blocks() {
    let mut driver = Fake {
        verdicts: vec![("no_verdict", 0), ("ACCEPT", 0)],
        ..fake()
    };
    assert_eq!(run(&mut driver, json!({}), "brief")["exit_code"], 0);
    assert_eq!(driver.reviews.len(), 2);
    let mut driver = Fake {
        review_head: Some("older-head"),
        ..fake()
    };
    let record = run(&mut driver, json!({}), "brief");
    assert_eq!(record["exit_code"], 4);
    assert_eq!(driver.reviews.len(), REVIEW_TRIES);
    assert!(record["reason"]
        .as_str()
        .unwrap()
        .starts_with("weak_verdict"));
}
#[test]
fn batch_driver_relaunch_keeps_open_findings_and_round_counts() {
    // Writer crashes during the fix turn after one REQUEST_CHANGES round.
    let mut driver = Fake {
        verdicts: vec![("REQUEST_CHANGES", 2)],
        ..fake()
    };
    let record = {
        let mut wrapped = CrashOnSecond { inner: &mut driver };
        run(&mut wrapped, json!({}), "brief")
    };
    assert_eq!(record["exit_code"], 0);
    assert_eq!(driver.calls, vec![None, Some("same-session".into()), None]);
    assert!(driver.prompts[2].contains("Open findings of the last review"));
    assert!(driver.prompts[2].contains("HIGH a.rs:1 - finding"));
    assert_eq!(record["round_counts"], json!([2]));
    assert_eq!(record["manifest"]["try_count"], 2);
}
struct CrashOnSecond<'a> {
    inner: &'a mut Fake,
}
impl Driver for CrashOnSecond<'_> {
    fn prepare(&mut self) -> Result<String, String> {
        self.inner.prepare()
    }
    fn refresh_base(&mut self) -> Result<String, String> {
        self.inner.refresh_base()
    }
    fn disk_gb(&mut self) -> Result<u64, String> {
        self.inner.disk_gb()
    }
    fn execute(&mut self, session: Option<&str>, prompt: &str) -> Result<String, String> {
        let result = self.inner.execute(session, prompt);
        if self.inner.calls.len() == 2 {
            return Err("codex killed: SIGKILL".into());
        }
        result
    }
    fn head(&mut self) -> Result<String, String> {
        self.inner.head()
    }
    fn fast_checks(&mut self, head: &str) -> Result<(), String> {
        self.inner.fast_checks(head)
    }
    fn stamped(&mut self, head: &str) -> bool {
        self.inner.stamped(head)
    }
    fn review(
        &mut self,
        head: &str,
        previous: Option<&serde_json::Value>,
    ) -> Result<serde_json::Value, String> {
        self.inner.review(head, previous)
    }
    fn gates(&mut self) -> Result<(), String> {
        self.inner.gates()
    }
    fn save(&mut self, record: &serde_json::Value) -> Result<(), String> {
        self.inner.save(record)
    }
}
#[test]
fn round_stop_is_the_round_progress_rule_capped_at_max_rounds() {
    assert_eq!(round_stop(&[3]), None);
    assert_eq!(round_stop(&[3, 2, 1, 0]), None);
    assert_eq!(round_stop(&[3, 3, 3]), Some("no_progress"));
    assert_eq!(round_stop(&[5, 4, 3, 2, 1]), None);
    assert_eq!(round_stop(&[16, 8, 7, 6, 5, 4, 3, 2]), Some("round_cap"));
    assert_eq!(
        high_med("HIGH a.rs:1 x\n- MED b.rs:2 y\nLOW c\nVERDICT: REQUEST_CHANGES\n"),
        2
    );
}
#[path = "native_writer_idle_tests.rs"]
mod idle;
#[path = "native_writer_placement_tests.rs"]
mod placement;
#[path = "native_writer_push_tests.rs"]
mod push;
