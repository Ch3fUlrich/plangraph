//! End-to-end Python admission -> Rust worker -> native CLI, without a model seat.
#![cfg(unix)]
use std::{fs, os::unix::fs::PermissionsExt, path::Path, process::Command};
fn git(repo: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().into()
}
#[test]
fn native_writer_pushed_base_handshake_review_resume_and_record() {
    native_writer_fixture(false);
}
#[test]
fn native_writer_ignores_host_admission_policy() {
    native_writer_fixture(true);
}
fn native_writer_fixture(hostile_policy: bool) {
    let dir = std::env::temp_dir().join(format!(
        "native-writer-e2e-{}-{hostile_policy}",
        std::process::id()
    ));
    fs::create_dir_all(&dir).unwrap();
    let remote = dir.join("remote.git");
    let repo = dir.join("checkout");
    fs::create_dir_all(&repo).unwrap();
    git(&dir, &["init", "--bare", remote.to_str().unwrap()]);
    git(&repo, &["init", "-b", "main"]);
    git(
        &repo,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
    );
    git(
        &repo,
        &["remote", "add", "origin", remote.to_str().unwrap()],
    );
    git(&repo, &["push", "origin", "main"]);
    let base = git(&repo, &["rev-parse", "HEAD"]);
    let bin = dir.join("bin");
    fs::create_dir_all(&bin).unwrap();
    let codex = bin.join("codex");
    fs::write(&codex, r#"#!/usr/bin/env python3
import sys, pathlib, subprocess, json
prompt = sys.stdin.read()
assert 'brief marker' in prompt or 'no required review' in prompt
assert 'model_reasoning_effort=medium' in sys.argv
assert sys.argv[sys.argv.index('-m') + 1] == 'gpt-6.1-sol'
if 'hold marker' in prompt:
    import os, time
    child = subprocess.Popen(['sleep', '30'])
    pathlib.Path('hold.pid').write_text(str(os.getpid()) + ' ' + str(child.pid))
    time.sleep(30)
    sys.exit(0)
if 'resume' in sys.argv:
    assert sys.argv[sys.argv.index('resume') + 1] == 'fixture-session'
    head = subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()
    pathlib.Path('reviews', head[:8] + '-hydrogen.txt').write_text('VERDICT: ACCEPT\n')
else:
    pathlib.Path('writer.txt').write_text('written on selected host\n')
    subprocess.run(['git','add','writer.txt'], check=True)
    subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','writer'], check=True, stdout=subprocess.DEVNULL)
    print(json.dumps({'type':'thread.started','thread_id':'fixture-session'}))
"#).unwrap();
    fs::set_permissions(&codex, fs::Permissions::from_mode(0o755)).unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut python = Command::new("python3");
    if hostile_policy {
        // Host disk policy must not divert this local lifecycle fixture into a queue.
        let config = dir.join("overflow-hosts.json");
        fs::write(
            &config,
            r#"{"hosts":{"fixture":{"orchestration_only":true}}}"#,
        )
        .unwrap();
        python
            .env("PLANGRAPH_DISK_FLOOR_GB", "1000000000")
            .env("PLANGRAPH_HOST_NAME", "fixture")
            .env("PLANGRAPH_OVERFLOW_HOSTS", config);
    }
    let out = python.current_dir(&root).env("PYTHONPATH", root.join("src"))
        .env("PATH", format!("{}:{}", bin.display(), std::env::var("PATH").unwrap()))
        .env("NATIVE_BINARY", env!("CARGO_BIN_EXE_pg-spawnd"))
        .env("FIXTURE_DIR", &dir).env("FIXTURE_BASE", &base)
        .args(["-c", r#"
import asyncio, json, os, pathlib
from unittest.mock import patch
from plangraph.spawner.service import SpawnService
from plangraph.spawner.base_fresh import admitted_spawn
from plangraph.spawner import overflow, seat_spawn
root = pathlib.Path(os.environ['FIXTURE_DIR'])
async def check():
    service = SpawnService(state=root/'state')
    card = dict(run_kind='native-codex', repo=str(root/'checkout'), base_ref='refs/heads/main')
    with patch('plangraph.fleet_snapshot._projection_binary', return_value=os.environ['NATIVE_BINARY']), patch.object(seat_spawn, 'resources', return_value=(0, 50000)):
        run = await admitted_spawn(service, dict(card=card, prompt='brief marker'))
        assert run.get('base_admitted') or run['status'] != 'running', run
        assert run['run_id'] in service.tasks, run
        await service.tasks[run['run_id']]
    record = service.result(run['run_id'])
    assert record['status'] == 'completed', record
    assert record['base_sha'] == os.environ['FIXTURE_BASE'], record
    assert record['final_head'] != record['base_sha'], record
    assert record['host'] and record['exit_code'] == 0, record
    assert record['session_id'] == 'fixture-session' and record['resumes'] == 1, record
    assert record['review']['verdict'] == 'ACCEPT' and record['review']['head'] == record['final_head'], record
    assert pathlib.Path(record['cwd']).is_dir(), record
    # A remote ref that is absent must never resolve the local-only branch.
    subprocess = __import__('subprocess')
    assert not pathlib.Path(record['cwd']).is_relative_to(pathlib.Path(card['repo'])), record
    assert subprocess.check_output(['git','-C',card['repo'],'status','--porcelain'], text=True) == ''
    subprocess.run(['git','-C',card['repo'],'branch','local-only'], check=True)
    card['base_ref'] = 'refs/heads/local-only'
    with patch('plangraph.fleet_snapshot._projection_binary', return_value=os.environ['NATIVE_BINARY']), patch.object(seat_spawn, 'resources', return_value=(0, 50000)):
        bad = await admitted_spawn(service, dict(card=card, prompt='brief marker'))
        await service.tasks[bad['run_id']]
    assert service.result(bad['run_id'])['exit_code'] == 5
    card['base_ref'] = 'refs/heads/main'
    with patch('plangraph.fleet_snapshot._projection_binary', return_value=os.environ['NATIVE_BINARY']), patch.object(seat_spawn, 'resources', return_value=(0, 50000)):
        holding = await admitted_spawn(service, dict(card=card, prompt='brief marker hold marker'))
        holding = service.result(holding['run_id'])
        pidfile = pathlib.Path(holding['cwd'])/'hold.pid'
        for _ in range(500):
            if pidfile.exists(): break
            await asyncio.sleep(.01)
        assert pidfile.exists(), holding
        assert holding['cwd'] != record['cwd'], holding
        assert subprocess.check_output(['git','-C',card['repo'],'status','--porcelain'], text=True) == ''
        codex_pid, tool_pid = map(int, pidfile.read_text().split())
        try:
            service.tasks[holding['run_id']].cancel()
            await asyncio.gather(service.tasks[holding['run_id']], return_exceptions=True)
            for _ in range(100):
                stat = subprocess.run(['ps','-o','stat=','-p',str(tool_pid)], capture_output=True, text=True).stdout.strip()
                if not stat or stat.startswith('Z'): break
                await asyncio.sleep(.01)
            assert not stat or stat.startswith('Z'), 'native cancellation left Codex tools alive'
        finally:
            import signal
            try: os.killpg(codex_pid, signal.SIGKILL)
            except ProcessLookupError: pass
# Exercise admission with fixed local policy; installed host policy may queue or
# remotely place the run, neither of which has a local supervisor task.
with patch.object(overflow, 'placement_policy', return_value=('fixture', False)), patch.object(overflow, 'local_limits', return_value=(2048, 3000)), patch('plangraph.spawner.host_ready.local_disk_refusal', return_value=None):
    asyncio.run(check())
"#]).output().unwrap();
    assert!(
        out.status.success(),
        "stdout={}\nstderr={}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    fs::remove_dir_all(dir).unwrap();
}
