//! End-to-end Python admission -> Rust worker -> native CLI, without a model seat.
#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;
use std::{fs, os::unix::fs::PermissionsExt, path::Path, process::Command};
fn git(repo: &Path, args: &[&str]) -> String {
    let out = pg_worker_core::git_test_support::command(repo)
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
// Re-exec instead of changing process-global Git variables beside parallel tests.
#[test]
fn hook_git_environment_leaves_sentinel_unchanged() {
    use pg_worker_core::git_test_support::command;
    const GIT_ENTRY: &str = ".git";
    let sentinel = tempfile::tempdir().unwrap();
    for args in [
        vec!["init", "-qb", "main"],
        vec!["commit", "--allow-empty", "-qm", "sentinel"],
    ] {
        assert!(command(sentinel.path())
            .args(args)
            .status()
            .unwrap()
            .success());
    }
    let snapshot = || {
        let mut bytes = fs::read(sentinel.path().join(".git/config")).unwrap();
        for args in [
            vec!["rev-parse", "HEAD"],
            vec!["worktree", "list", "--porcelain"],
        ] {
            let output = command(sentinel.path()).args(args).output().unwrap();
            assert!(output.status.success());
            bytes.extend(output.stdout);
        }
        bytes
    };
    let before = snapshot();
    let fixture = tempfile::tempdir().unwrap();
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "hook_git_environment_fixture_child",
            "--nocapture",
        ])
        .env("PG_GIT_ISOLATION_FIXTURE", fixture.path())
        .env("GIT_DIR", sentinel.path().join(GIT_ENTRY))
        .env("GIT_WORK_TREE", sentinel.path())
        .env("GIT_COMMON_DIR", sentinel.path().join(GIT_ENTRY))
        .env("GIT_INDEX_FILE", sentinel.path().join(".git/index"))
        .output()
        .unwrap();
    assert_eq!(
        snapshot(),
        before,
        "fixture mutated sentinel config, HEAD or worktrees"
    );
    assert!(output.status.success(), "{output:?}");
    assert!(fixture.path().join(".git/config").is_file());
}

#[test]
fn hook_git_environment_fixture_child() {
    let Some(root) = std::env::var_os("PG_GIT_ISOLATION_FIXTURE") else {
        return;
    };
    let root = Path::new(&root);
    git(root, &["init", "-qb", "main"]);
    git(root, &["config", "user.name", "fixture only"]);
    git(root, &["config", "user.email", "fixture@example.invalid"]);
    git(
        root,
        &[
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
    );
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
    fs::create_dir_all(repo.join("tools")).unwrap();
    fs::write(
        repo.join("tools/check_file_size.py"),
        r#"import json, os, pathlib, subprocess
record = json.loads((pathlib.Path.cwd().parent / 'record.json').read_text())
assert os.environ['PATH'] == record['tool_path']
assert pathlib.Path('writer-path').read_text() == record['tool_path']
assert subprocess.check_output(['cargo'], text=True).strip() == 'fixture cargo'
for tool in ('cargo', 'git', 'codex'):
    import shutil
    assert shutil.which(tool) == record['resolved_tools'][tool]
"#,
    )
    .unwrap();
    for gate in ["check_new_code_lang.py", "check_changelog_docs.py"] {
        fs::write(repo.join("tools").join(gate), "").unwrap();
    }
    git(&repo, &["add", "tools"]);
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
if 'kill marker' in prompt or 'fail marker' in prompt or 'missing marker' in prompt:
    import os, signal
    attempts = pathlib.Path('attempts')
    attempt = int(attempts.read_text()) + 1 if attempts.exists() else 1
    attempts.write_text(str(attempt))
    if 'fail marker' in prompt:
        sys.exit(127)
    if attempt == 1:
        if 'missing marker' in prompt:
            print(json.dumps({'type':'thread.started','thread_id':'incomplete-session'}))
            sys.exit(0)
        pathlib.Path('partial.txt').write_text('preserved writer work')
        subprocess.run(['git','add','partial.txt'], check=True)
        subprocess.run(['git','commit','-m','partial writer work'], check=True, stdout=subprocess.DEVNULL)
        checkout = pathlib.Path(os.environ['FIXTURE_DIR'])/'checkout'
        (checkout/'fresh.txt').write_text('new main base')
        subprocess.run(['git','-C',str(checkout),'add','fresh.txt'], check=True)
        subprocess.run(['git','-C',str(checkout),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fresh main'], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['git','-C',str(checkout),'push','origin','main'], check=True, stdout=subprocess.DEVNULL)
        print(json.dumps({'type':'thread.started','thread_id':'killed-session'}), flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    if 'kill marker' in prompt:
        assert pathlib.Path('fresh.txt').read_text() == 'new main base'
        assert pathlib.Path('partial.txt').read_text() == 'preserved writer work'

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
    pathlib.Path('reviews', head[:8] + '-hydrogen.txt').write_text('Reviewed writer.txt\nVERDICT: ACCEPT\n')
else:
    import os
    pathlib.Path('writer-path').write_text(os.environ['PATH'])
    pathlib.Path('writer.txt').write_text('written on selected host\n')
    subprocess.run(['git','add','writer.txt','writer-path'], check=True)
    subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','writer'], check=True, stdout=subprocess.DEVNULL)
    print(json.dumps({'type':'thread.started','thread_id':'fixture-session'}))
if 'reject marker' in prompt:
    head = subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()
    pathlib.Path('reviews', head[:8] + '-rejected.txt').write_text('VERDICT: REQUEST_CHANGES\n')
    sys.exit(1)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1000,'output_tokens':234}}))
print('tokens used\n1,234')
"#).unwrap();
    fs::set_permissions(&codex, fs::Permissions::from_mode(0o755)).unwrap();
    fs::create_dir_all(dir.join("supervisor-bin")).unwrap();
    // The fixture does not depend on free space on the host's /tmp mount.
    let df = bin.join("df");
    fs::write(&df, "#!/bin/sh\nprintf 'fixture 0 0 10485760 0%% /\\n'\n").unwrap();
    fs::set_permissions(&df, fs::Permissions::from_mode(0o755)).unwrap();
    fs::copy(&df, dir.join("supervisor-bin/df")).unwrap();
    let cargo = bin.join("cargo");
    fs::write(&cargo, "#!/bin/sh\necho 'fixture cargo'\n").unwrap();
    fs::set_permissions(&cargo, fs::Permissions::from_mode(0o755)).unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let home = dir.join("home");
    let runtime = home.join("runtime");
    fs::create_dir_all(&runtime).unwrap();
    let mut python = Command::new("python3");
    // Drop caller Git redirects first; the fixture HOME and Git config below override its paths.
    pg_worker_core::git_test_support::configure(&mut python, &dir);
    for (key, _) in std::env::vars_os() {
        let name = key.to_string_lossy();
        if name.starts_with("AUTOOS_")
            || name.starts_with("PLANGRAPH_")
            || name.starts_with("XDG_")
            || name.starts_with("GIT_CONFIG")
            || matches!(
                name.as_ref(),
                "PYTHONHOME" | "PYTHONUSERBASE" | "PYTHONSTARTUP"
            )
        {
            python.env_remove(&key);
        }
    }
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
        .env("HOME", &home).env("XDG_CONFIG_HOME", home.join(".config"))
        .env("XDG_STATE_HOME", home.join(".local/state")).env("XDG_DATA_HOME", home.join(".local/share"))
        .env("XDG_CACHE_HOME", home.join(".cache")).env("XDG_RUNTIME_DIR", &runtime)
        .env("GIT_CONFIG_NOSYSTEM", "1").env("GIT_CONFIG_GLOBAL", home.join(".gitconfig")).env("FIXTURE_HOME", &home)
        .env("PATH", format!("{}:{}", bin.display(), std::env::var("PATH").unwrap()))
        .env("GIT_AUTHOR_NAME", "Fixture").env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
        .env("GIT_COMMITTER_NAME", "Fixture").env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
        .env("NATIVE_BINARY", binary::spawnd())
        .env("FIXTURE_DIR", &dir).env("FIXTURE_BASE", &base)
        .args(["-c", r#"
import asyncio, json, os, pathlib
from unittest.mock import patch
from plangraph.spawner.service import SpawnService
from plangraph.spawner.base_fresh import admitted_spawn
from plangraph.spawner import overflow, seat_spawn
root = pathlib.Path(os.environ['FIXTURE_DIR'])
original_exec = asyncio.create_subprocess_exec
async def different_supervisor_path(*args, **kwargs):
    if len(args) > 1 and args[1] == 'native-writer':
        kwargs['env'] = dict(kwargs.get('env', os.environ), PATH=str(root/'supervisor-bin') + ':/usr/bin:/bin')
    return await original_exec(*args, **kwargs)
# Isolation: no host config, state or credentials can be reached through HOME or XDG.
assert pathlib.Path.home() == pathlib.Path(os.environ['FIXTURE_HOME']), pathlib.Path.home()
for name in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR'):
    assert os.path.realpath(os.environ[name]).startswith(os.path.realpath(root) + os.sep), (name, os.environ[name])
# Keys the fixture sets itself (isolation paths and, in the hostile-policy variant, host policy).
FIXTURE_KEYS = {'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR', 'PLANGRAPH_DISK_FLOOR_GB', 'PLANGRAPH_HOST_NAME', 'PLANGRAPH_OVERFLOW_HOSTS'}
assert not [k for k in os.environ if k.startswith(('AUTOOS_', 'PLANGRAPH_', 'XDG_', 'GIT_CONFIG')) and k not in FIXTURE_KEYS], sorted(os.environ)
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
    for marker, tries, status in [('kill marker', 2, 'completed'), ('fail marker', 3, 'failed'), ('missing marker', 2, 'completed'), ('reject marker', 1, 'failed')]:
        with patch('plangraph.fleet_snapshot._projection_binary', return_value=os.environ['NATIVE_BINARY']), patch.object(seat_spawn, 'resources', return_value=(0, 50000)):
            retried = await admitted_spawn(service, dict(card=card, prompt='brief marker ' + marker))
            await service.tasks[retried['run_id']]
        final = service.result(retried['run_id'])
        assert final['status'] == status, final
        assert final['manifest']['try_count'] == tries, final
        assert final['manifest']['brief'] == 'brief marker ' + marker, final
        assert final['manifest']['worktree'] == final['cwd'], final
        if marker == 'reject marker':
            assert final['exit_code'] == 3 and final['review']['verdict'] == 'REQUEST_CHANGES', final
        elif status == 'failed':
            assert final['result'] == 'BLOCKED' and final['exit_code'] == 9, final
        else:
            assert final['session_id'] == 'fixture-session', final
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
with patch('asyncio.create_subprocess_exec', side_effect=different_supervisor_path), patch('plangraph.spawner.host_ready.disk_free_gb', return_value=10), patch.object(overflow, 'placement_policy', return_value=('fixture', False)), patch.object(overflow, 'local_limits', return_value=(2048, 3000)), patch('plangraph.spawner.host_ready.local_disk_refusal', return_value=None):
    asyncio.run(check())
"#]).output().unwrap();
    assert!(
        out.status.success(),
        "stdout={}\nstderr={}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    let mut completed = 0;
    for entry in fs::read_dir(dir.join("state/runs")).unwrap() {
        let path = entry.unwrap().path().join("ledger.jsonl");
        if !path.exists() {
            continue;
        }
        let row: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
        if row["exit_code"] == 0 {
            completed += 1;
            assert_eq!(row["tokens"], 2468); // Two CLI executions, each logs 1,234.
            assert_eq!(row["changed_lines"], 1);
            assert_eq!(row["review_rounds"], 1);
            assert!(row["wall_minutes"].as_f64().unwrap() > 0.0);
        }
    }
    assert_eq!(completed, 1);
    fs::remove_dir_all(dir).unwrap();
}
