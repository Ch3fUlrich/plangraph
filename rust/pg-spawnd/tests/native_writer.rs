//! End-to-end Python admission -> Rust worker -> native CLI, without a model seat.
#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;
#[path = "native_writer_fixture/codex.rs"]
mod codex_fixture;
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
    fs::write(&codex, codex_fixture::CODEX).unwrap();
    std::os::unix::fs::symlink(binary::spawnd(), bin.join("fixture-spawnd")).unwrap();
    fs::set_permissions(&codex, fs::Permissions::from_mode(0o755)).unwrap();
    // Stub spawner CLI: one review run per call, its verdict in the run's live.log.
    let review_cli = dir.join("review-cli.py");
    fs::write(&review_cli, r#"import json, pathlib, subprocess, sys, time, uuid
argv = sys.argv[1:]
value = lambda flag: argv[argv.index(flag) + 1]
assert value('--role') == 'review' and value('--exclude-family') == 'openai', argv
assert value('--leg') == 'claude-sonnet-5-5-high', argv
assert value('--override-reason') == 'D-6550 fixture credential review' and '--no-fallthrough' in argv, argv
card = json.loads(value('--card'))
head = card['diff_ref']['head']
prompt = pathlib.Path(value('--prompt-file')).read_text()
run_id = 'run-' + uuid.uuid4().hex
run = pathlib.Path(value('--state')) / 'runs' / run_id
run.mkdir(parents=True)
assert card['review_workspace'] and '--cwd' not in argv
marker = subprocess.check_output(['git', 'show', head + ':writer.txt'], text=True)
if 'late marker' in marker:
    receipt = dict(run_id=run_id, status='running', started=time.time())
    (run / 'record.json').write_text(json.dumps(receipt))
    print(json.dumps(receipt))
    sys.exit(0)
if 'client marker' in marker and '----- previous review -----' in prompt:
    assert 'HIGH writer.txt:1 - fixture finding' in prompt, prompt
    print(json.dumps(dict(run_id=run_id, status='failed',
        stderr_tail=f'REVIEW {run_id} FAIL client_missing /fixture/codex')))
    sys.exit(10)
if 'reject marker' in marker or 'client marker' in marker:
    text = 'HIGH writer.txt:1 - fixture finding\nHEAD: ' + head + '\nVERDICT: REQUEST_CHANGES\n'
else:
    fixed = subprocess.run(['git', 'cat-file', '-e', head + ':fix.txt'], capture_output=True).returncode == 0
    tests = 'TESTS: cargo test -p fixture — 1 passed\n' if fixed else ''
    text = 'FILES READ: writer.txt\n' + tests + 'HEAD: ' + head + '\nVERDICT: ACCEPT\n'
(run / 'live.log').write_text(json.dumps({'type':'result','is_error':False,'result':text}) + '\n')
print(json.dumps({'run_id':run_id,'status':'completed','head_sha':head,'host':'fixture-worker','overflow_remote':True,'stdout_tail':text,'chosen_leg':{'id':'claude-sonnet-5-5-high','harness':'claude','tool_mode':'tools'}}))
"#).unwrap();
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
        .env("PLANGRAPH_NATIVE_REVIEW_CLI", serde_json::json!(["python3", review_cli]).to_string())
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
FIXTURE_KEYS = {'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR', 'PLANGRAPH_DISK_FLOOR_GB', 'PLANGRAPH_HOST_NAME', 'PLANGRAPH_OVERFLOW_HOSTS', 'PLANGRAPH_NATIVE_REVIEW_CLI'}
assert not [k for k in os.environ if k.startswith(('AUTOOS_', 'PLANGRAPH_', 'XDG_', 'GIT_CONFIG')) and k not in FIXTURE_KEYS], sorted(os.environ)
async def check():
    service = SpawnService(state=root/'state')
    card = dict(run_kind='native-codex', repo=str(root/'checkout'), base_ref='refs/heads/main',
                review_leg='claude-sonnet-5-5-high', review_override_reason='D-6550 fixture credential review')
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
    assert record['session_id'] == 'fixture-session' and record['resumes'] == 0, record
    assert record['review']['verdict'] == 'ACCEPT' and record['review']['head'] == record['final_head'], record
    assert record['review_test_status'] == 'untested', record
    review_text = pathlib.Path(record['review']['path']).read_text()
    assert [line for line in review_text.splitlines() if line.startswith('TESTS:')] == [
        'TESTS: none: reviewer supplied no test result'], review_text
    assert record['review']['leg'] == 'claude-sonnet-5-5-high' and record['review']['harness'] == 'claude' and record['status_line'] == 'DONE', record
    assert record['fast_checks'] == {'head': record['final_head'], 'status': 'passed'}, record
    assert pathlib.Path(record['cwd']).is_dir(), record
    # A remote ref that is absent must never resolve the local-only branch.
    subprocess = __import__('subprocess')
    prompt_file = root/'external-fix.md'
    prompt_file.write_text('brief marker external fix')
    resumed = subprocess.run([os.environ['NATIVE_BINARY'], 'resume', run['run_id'],
        '--state', str(root/'state'), '--prompt-file', str(prompt_file)], capture_output=True, text=True)
    assert resumed.returncode == 0, resumed.stderr + (pathlib.Path(record["cwd"]).parent / "native.log").read_text()
    fixed = service.result(run['run_id'])
    assert fixed['status'] == 'completed' and fixed['final_head'] != record['final_head'], fixed
    for field in ('run_id', 'session_id', 'cwd', 'tool_path', 'review_dir'):
        assert fixed[field] == record[field], (field, fixed)
    assert fixed['review']['head'] == fixed['final_head'], fixed
    assert fixed['review']['run_id'] != record['review']['run_id'], fixed
    assert fixed['resume_from']['head'] == record['final_head'], fixed
    assert fixed['fast_checks'] == {'head': fixed['final_head'], 'status': 'passed'}, fixed
    assert fixed['review_test_status'] == 'reported', fixed
    ledger = pathlib.Path(fixed['cwd']).parent/'ledger.jsonl'
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]['tokens'] == rows[1]['tokens'] == 1234, rows
    assert not pathlib.Path(record['cwd']).is_relative_to(pathlib.Path(card['repo'])), record
    assert subprocess.check_output(['git','-C',card['repo'],'status','--porcelain'], text=True) == ''
    subprocess.run(['git','-C',card['repo'],'branch','local-only'], check=True)
    for ref, exit_code in [('refs/heads/local-only', 0), ('refs/heads/absent', 5)]:
        card['base_ref'] = ref
        with patch('plangraph.fleet_snapshot._projection_binary', return_value=os.environ['NATIVE_BINARY']), patch.object(seat_spawn, 'resources', return_value=(0, 50000)):
            other = await admitted_spawn(service, dict(card=card, prompt='brief marker'))
            await service.tasks[other['run_id']]
        record = service.result(other['run_id'])
        assert record['exit_code'] == exit_code, record
        assert exit_code == 0 or ref in record['reason'], record
    card['base_ref'] = 'refs/heads/main'
    for marker, tries, status in [('kill marker', 2, 'completed'), ('fail marker', 3, 'failed'), ('missing marker', 2, 'completed'), ('reject marker', 1, 'failed')]:
        brief = 'brief marker ' + marker + ('\nWhy: fixture item\n# Run values\nNAME: fixture-batch\n' if marker == 'reject marker' else '')
        with patch('plangraph.fleet_snapshot._projection_binary', return_value=os.environ['NATIVE_BINARY']), patch.object(seat_spawn, 'resources', return_value=(0, 50000)):
            retried = await admitted_spawn(service, dict(card=card, prompt=brief))
            await service.tasks[retried['run_id']]
        final = service.result(retried['run_id'])
        assert final['status'] == status, final
        assert final['manifest']['try_count'] == tries, final
        assert final['manifest']['brief'] == brief, final
        assert final['manifest']['worktree'] == final['cwd'], final
        if marker == 'reject marker':
            assert final['exit_code'] == 3 and final['review']['verdict'] == 'REQUEST_CHANGES', final
            assert final['round_stop'] == 'no_progress' and final['review_rounds'] == 3, final
            assert final['resumes'] == 2 and final['session_id'] == 'fixture-session', final
            messages = final['steer_messages']
            assert len(messages) == 1 and messages[0]['message'] == 'drop the fixture scope', messages
            assert messages[0]['delivered_at'], messages
            denied = subprocess.run([os.environ['NATIVE_BINARY'], 'steer-local', final['run_id'],
                '--state', str(root/'state')], input='too late', text=True, capture_output=True)
            assert json.loads(denied.stdout)['error'] == 'native writer is finished', denied.stdout
        elif status == 'failed':
            assert final['result'] == 'BLOCKED' and final['exit_code'] == 9, final
        else:
            assert final['session_id'] == 'fixture-session', final
    for marker in ('client marker', 'late marker'):
        with patch('plangraph.fleet_snapshot._projection_binary', return_value=os.environ['NATIVE_BINARY']), patch.object(seat_spawn, 'resources', return_value=(0, 50000)):
            reviewed = await admitted_spawn(service, dict(card=card, prompt='brief marker ' + marker))
            await service.tasks[reviewed['run_id']]
        final = service.result(reviewed['run_id'])
        if marker == 'client marker':
            assert final['exit_code'] == 10 and final['reason'].endswith('FAIL client_missing /fixture/codex'), final
            assert final['review']['verdict'] == 'REQUEST_CHANGES', final
            assert final['review']['head'] != final['final_head'], final
            assert 'HIGH writer.txt:1' in final['review']['text'], final
            assert final['review_rounds'] == 1 and final['resumes'] == 1, final
            assert final['session_id'] == 'fixture-session', final
            assert pathlib.Path(final['review']['path']).read_text() == final['review']['text'], final
        else:
            head = final['final_head']
            pending = pathlib.Path(final['review_dir']) / f'late-review-{head}.json'
            receipt = json.loads(pending.read_text())
            assert final['exit_code'] == 4 and receipt['run_id'] in final['reason'], final
            assert 'expected end' in final['reason'], final
            run_file = root/'state'/'runs'/receipt['run_id']/'record.json'
            run = json.loads(run_file.read_text())
            run.update(status='completed', host='fixture-worker', head_sha=head, overflow_remote=True,
                       chosen_leg={'id':'claude-sonnet-5-5-high','harness':'claude','tool_mode':'tools'},
                       stdout_tail='FILES READ: writer.txt\nVERDICT: ACCEPT\n')
            updated = run_file.with_suffix('.tmp')
            updated.write_text(json.dumps(run))
            updated.replace(run_file)
            artifact = pending.with_suffix('.txt')
            for _ in range(150):
                if artifact.is_file(): break
                await asyncio.sleep(.05)
            assert artifact.read_text() == (
                f'HEAD: {head}\nTESTS: none: reviewer supplied no test result\n'
                'FILES READ: writer.txt\nVERDICT: ACCEPT\n')
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
with patch('asyncio.create_subprocess_exec', side_effect=different_supervisor_path), patch('plangraph.spawner.host_ready.disk_free_gb', return_value=10), patch.object(overflow, 'placement_policy', return_value=('fixture', False)), patch.object(overflow, 'local_limits', return_value=(2048, 3000)), patch('plangraph.spawner.host_ready.local_disk_admission', return_value=(None, None)):
    asyncio.run(check())
"#]).output().unwrap();
    assert!(
        out.status.success(),
        "stdout={}\nstderr={}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    let mut completed = Vec::new();
    for entry in fs::read_dir(dir.join("state/runs")).unwrap() {
        let path = entry.unwrap().path().join("ledger.jsonl");
        if !path.exists() {
            continue;
        }
        let ledger = fs::read_to_string(path).unwrap();
        let row: serde_json::Value = serde_json::from_str(ledger.lines().next().unwrap()).unwrap();
        if row["exit_code"] == 0 {
            // One counted CLI execution per completed run logs 1,234.
            assert_eq!(row["tokens"], 1234);
            assert_eq!(row["review_rounds"], 1);
            assert!(row["wall_minutes"].as_f64().unwrap() > 0.0);
            completed.push(row["changed_lines"].as_u64().unwrap());
        }
    }
    // The plain, local-only-base and missing-marker runs commit writer.txt and
    // writer-path, one line each; the kill-marker relaunch also keeps the killed
    // attempt's partial.txt.
    completed.sort_unstable();
    assert_eq!(completed, [2, 2, 2, 3]);
    fs::remove_dir_all(dir).unwrap();
}
