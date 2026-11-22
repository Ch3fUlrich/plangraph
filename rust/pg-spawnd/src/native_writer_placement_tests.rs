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
            caps={'mem_floor_mb': floor}, warmup={'ramp_mb': {'implement': 1024, 'review': 512}}))
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
            patch.object(host_ready, 'local_disk_refusal', return_value=None)), (
            patch.object(overflow, 'remote_spawn', return_value=run)) as spawn:
        service = SpawnService(state=root / 'state')
        card = {'diff_ref': {'repo': checkout, 'base': 'abc', 'head': 'def'}}
        record = service.admit_spawn({'role': 'review', 'prompt': 'fixture'}, card=card)
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
    let output = test_python::command()
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
from pathlib import Path
import subprocess, tempfile
from plangraph.spawner.overflow import stage_review_diff
from plangraph.spawner.prompt_input import checkout_env

def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo),
        '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', 'commit.gpgsign=false', *args], text=True, env=checkout_env()).strip()
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    source, target = root / 'source', root / 'target'
    git(root, 'init', '-q', '-b', 'main', str(source))
    git(source, 'commit', '--allow-empty', '-qm', 'public base')
    public = git(source, 'rev-parse', 'HEAD')
    git(source, 'update-ref', 'refs/remotes/origin/main', public)
    git(root, 'clone', '-q', '--no-local', str(source), str(target))
    git(source, 'commit', '--allow-empty', '-qm', 'earlier unpushed review')
    base = git(source, 'rev-parse', 'HEAD')
    (source / 'change.txt').write_text('reviewed exactly')
    git(source, 'add', 'change.txt')
    git(source, 'commit', '-qm', 'unpushed head')
    head = git(source, 'rev-parse', 'HEAD')
    [bundle] = stage_review_diff({'repo': str(source), 'base': base, 'head': head}, root)
    git(target, 'fetch', '--no-write-fetch-head', '--no-tags', '--', str(bundle), 'refs/review/head')
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
