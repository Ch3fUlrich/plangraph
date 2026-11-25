//! Review placement from the three-host registry and review bundles on the receiving host.

use crate::test_python;

#[test]
fn registry_places_a_review_without_cwd_and_records_the_memory_reason() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
from datetime import datetime, timezone
import json, os, pathlib, tempfile
from unittest.mock import patch
from plangraph.spawner import overflow, seat_spawn, host_ready
from plangraph.spawner.service import SpawnService

with tempfile.TemporaryDirectory() as directory:
    root = pathlib.Path(directory)
    config = root / 'config' / 'plangraph'
    config.mkdir(parents=True)
    ready = root / '.local/state/plangraph/host-ready'
    ready.mkdir(parents=True)
    checkout = str(root / 'public')
    hosts = []
    for name, free, floor in [('local', 30000, 3000), ('worker', 19000, 12000), ('windows-hop', 15000, 4000)]:
        hosts.append(dict(id=name, transport='local' if name == 'local' else 'ssh', os='linux',
            placement_fallback=name == 'local', state_dir='/state', roles=['implement', 'review'],
            repos={'plangraph': {'root': checkout if name == 'local' else '/repo/' + name}},
            remote_placement=True, caps={'mem_floor_mb': floor}, warmup={'ramp_mb': {'implement': 1024, 'review': 512}}))
        row = dict(host=name, mem_available_mb=free, live_runs=0, disk_free_gb=20,
                   ok=True, checked_at=datetime.now(timezone.utc).isoformat())
        (ready / (name + '.json')).write_text(json.dumps(row))
    hosts[2]['wsl_distro'] = 'Ubuntu'
    (config / 'hosts.json').write_text(json.dumps(dict(version=1, self='local',
        hosts=hosts, public_repos=['plangraph'])))
    (config / 'overflow-hosts.json').write_text(json.dumps({'hosts': {'unregistered': {}}}))
    run = dict(run_id='run-review', role='review', status='running')
    with patch.dict(os.environ, HOME=directory, USERPROFILE=directory, XDG_CONFIG_HOME=str(config.parent),
                    PLANGRAPH_HOST_NAME='local'), patch.object(seat_spawn, 'resources', return_value=(0, 30000)), (
            patch.object(host_ready, 'local_disk_admission', return_value=(None, None))) as disk, (
            patch.object(overflow, 'remote_spawn', return_value=run)) as spawn:
        service = SpawnService(state=root / 'state')
        card = {'diff_ref': {'repo': checkout, 'base': 'abc', 'head': 'def'}}
        record = service.admit_spawn({'role': 'review', 'prompt': 'fixture'}, card=card)
        disk.assert_called_once_with(pathlib.Path(checkout).resolve(), service, 'review', movable=False)
        host, route, sent, *_ = spawn.call_args.args
        assert host == 'windows-hop' and route['wsl_distro'] == 'Ubuntu', spawn.call_args
        assert sent == card and 'cwd' not in sent, sent
        persisted = service.result(record['run_id'])
        assert persisted['placement'] == dict(host=host, reason='11000 MB above memory floor', free_mb=15000), persisted
"#])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[cfg(unix)]
#[test]
fn overflow_review_bundle_materializes_unpublished_commits_on_receiver() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let folder = tempfile::tempdir().unwrap();
    let mut python = test_python::command();
    let executable = python.get_program().to_string_lossy();
    let script = r#"from pathlib import Path
import json, os, sys, tempfile
from plangraph.spawner.prompt_input import checkout_review

args = sys.argv
diff = json.loads(Path(args[args.index('--card') + 1][1:]).read_text())['diff_ref']
assert Path(diff['repo']) == Path.cwd()
with tempfile.TemporaryDirectory() as directory:
    checkout = Path(directory) / 'checkout'
    head = checkout_review(diff, checkout)
    assert (checkout / 'change.txt').read_text() == 'reviewed exactly'
print(json.dumps({'run_id':'run-fixture', 'status':'completed', 'head_sha':head,
                  'host':os.environ['PLANGRAPH_PLACED_HOST']}))
"#;
    pg_worker_core::executable_test_support::write_private_executable(
        folder.path().join("python3"),
        format!("#!{executable}\n{script}"),
    );
    let output = python
        .current_dir(&root)
        .env("PG_FIXTURE_PYTHON_BIN", folder.path())
        .env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
from pathlib import Path
import os, shlex, shutil, subprocess, sys, tempfile
from unittest.mock import patch
from plangraph.spawner.overflow import remote_spawn
from plangraph.spawner.prompt_input import checkout_env

def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo),
        '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', 'commit.gpgsign=false', *args], text=True, env=checkout_env()).strip()
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    source, target, origin = (root / name for name in ('source', 'target', 'origin'))
    git(root, 'init', '-q', '-b', 'main', str(source))
    git(source, 'commit', '--allow-empty', '-qm', 'public base')
    public = git(source, 'rev-parse', 'HEAD')
    git(source, 'update-ref', 'refs/remotes/origin/main', public)
    git(root, 'clone', '-q', '--bare', '--no-local', str(source), str(origin))
    git(root, 'clone', '-q', '--no-local', str(origin), str(target))
    git(source, 'commit', '--allow-empty', '-qm', 'earlier unpushed review')
    base = git(source, 'rev-parse', 'HEAD')
    (source / 'change.txt').write_text('reviewed exactly')
    git(source, 'add', 'change.txt')
    git(source, 'commit', '-qm', 'unpushed head')
    head = git(source, 'rev-parse', 'HEAD')
    absent = subprocess.run(['git', '-C', target, 'cat-file', '-e', head],
                            env=checkout_env(), capture_output=True)
    assert absent.returncode != 0
    folder = Path(os.environ['PG_FIXTURE_PYTHON_BIN'])
    run = subprocess.run
    def transport(argv, **options):
        if argv[0] == 'ssh':
            env = dict(os.environ, PATH=str(folder) + os.pathsep + os.environ['PATH'])
            return run(['/bin/sh', '-c', argv[-1]], env=env, **options)
        if argv[0] == 'scp':
            destination, = shlex.split(argv[-1].split(':', 1)[1])
            for path in argv[2:-1]:
                shutil.copy(path, destination)
            return subprocess.CompletedProcess(argv, 0)
        return run(argv, **options)

    host = {'ssh': 'fixture', 'registry': True, 'repo': str(target),
            'public_repo_ids': {'plangraph': str(source)}, 'repos': {'plangraph': str(target)},
            'spawner_src': str(Path.cwd() / 'src')}
    card = {'review_workspace': True, 'diff_ref': {'repo': str(source), 'base': base, 'head': head}}
    with patch('subprocess.run', side_effect=transport):
        receipt = remote_spawn('worker', host, card, 'fixture', ['--role', 'review'])
    assert receipt == {'run_id':'run-fixture', 'status':'completed', 'head_sha':head, 'host':'worker'}, receipt
    assert git(target, 'merge-base', base, head) == base
    assert git(target, 'show', head + ':change.txt') == 'reviewed exactly'
    assert git(target, 'rev-parse', 'HEAD') == public
"#])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
