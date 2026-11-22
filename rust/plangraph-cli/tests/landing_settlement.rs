#![cfg(unix)]

use std::{path::Path, process::Command};

#[test]
fn keeper_reprepares_an_open_train_but_preserves_terminal_manifests() {
    keeper_fixture(
        r#"
from plangraph.keeper.auth import Principal, add_principal
from plangraph.keeper.db import open_db
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store

db = open_db(':memory:', create=True, backend='sqlite')
add_principal(db, 'lander', 'router', [])
store = Store(db)
principal = Principal('lander', 'router', [])
repo = 'example/repo'
lease = store.land(principal, dict(repo=repo, action='acquire', owner='test'))

def land(action, pr, **fields):
    return store.land(principal, dict(lease, repo=repo, pr=pr, action=action, **fields))

original = dict(base='a'*40, head='b'*40, parts=[dict(pr=5, head='c'*40)], batch_key='first')
pushed_again = dict(base='d'*40, head='e'*40, parts=[dict(pr=6, head='f'*40)], batch_key='second')

def assert_unchanged(pr, before, attempted):
    try:
        land('prepare', pr, **attempted)
    except Refused as exc:
        assert exc.code == 'conflict', exc.code
    else:
        raise AssertionError('terminal train accepted a changed manifest')
    assert land('show', pr)['train'] == before

land('prepare', 17, **original)
reprepared = land('prepare', 17, **pushed_again)
assert reprepared['base'] == pushed_again['base']
assert reprepared['head'] == pushed_again['head']
assert reprepared['parts'] == [dict(pr=6, head='f'*40, items=[], ref='')]
assert reprepared['batch_key'] == 'second'
assert reprepared['merge'] is None
landed = land('landed', 17, merge='1'*40)
assert_unchanged(17, landed, original)
assert land('prepare', 17, **pushed_again) == landed
land('prepare', 18, **original)
abandoned = land('abandon', 18)
assert_unchanged(18, abandoned, pushed_again)
"#,
    );
}

#[test]
fn keeper_reopens_the_exact_landed_train_and_advances_each_step_once() {
    keeper_fixture(
        r#"
from pathlib import Path
import json
from tempfile import TemporaryDirectory
from plangraph.keeper.auth import Principal, add_principal
from plangraph.keeper.db import open_db
from plangraph.keeper.store import Store
from plangraph.keeper.errors import Refused
from unittest.mock import patch, Mock
import merge_batch_build as build
import merge_batch_land as landing

with TemporaryDirectory() as tmp:
    path = Path(tmp) / 'keeper.db'
    db = open_db(path, create=True, backend='sqlite')
    add_principal(db, 'lander', 'router', [])
    principal = Principal('lander', 'router', [])
    identity = dict(repo='example/repo', pr=17)
    lease = Store(db).land(principal, dict(repo=identity['repo'], action='acquire', owner='test'))
    identity.update(lease)
    train = dict(identity, base='a'*40, head='b'*40, parts=[{'pr': 5, 'head': 'c'*40}])
    Store(db).land(principal, dict(train, action='prepare'))
    db.close()

    db = open_db(path, backend='sqlite')
    store = Store(db)
    pending = store.land(principal, dict(repo=identity['repo'], action='pending'))['trains']
    assert len(pending) == 1 and pending[0]['merge'] is None
    def journal(repo, action, **fields):
        return store.land(principal, dict(fields, repo=repo, action=action))
    first = Store(db).land(principal, dict(identity, action='landed', merge='d'*40, included=[5]))
    assert first['open_steps'] == ['evidence', 'close_parts', 'host_sync', 'cleanup']
    calls = []
    proof = [dict(host=str(i), head='d'*40, checkout='d'*40, hooks='d'*40, build='d'*40, synced=i==1)
             for i in range(3)]
    def effect(args, **kwargs):
        calls.append(args)
        return Mock(stdout=json.dumps({'verified': proof}) if 'verify-hosts' in args else '', stderr='')
    st = dict(merged=train['parts'], head=train['head'])
    with (patch.object(landing, 'landing_journal', journal),
          patch.object(build, 'landing_journal', journal),
          patch.object(landing, 'read_verified_merge', return_value=('d'*40, 'merged')),
          patch.object(landing, 'run', effect),
          patch.object(landing, 'cleanup_landed', side_effect=lambda *args, **kw: calls.append('cleanup')),
          patch.object(landing, 'drop_state', side_effect=lambda st: calls.append('drop_state'))):
        landing.settle_train(pending[0], lease, st)
        landing.settle_train(pending[0], lease, st)
    assert calls == [['plangraph', 'merge-close'], ['plangraph', 'landing', 'verify-hosts', 'example/repo', 'd'*40],
                     'cleanup', 'drop_state'], calls
    recorded = store.land(principal, dict(identity, action='show'))['train']
    assert recorded['host_verification'] == proof
    assert recorded['included'] == [5]
    assert store.land(principal, dict(repo=identity['repo'], action='pending')) == {'trains': []}
    assert store.land(principal, dict(identity, action='landed', merge='d'*40))['open_steps'] == []
    try:
        store.land(principal, dict(identity, action='landed', merge='e'*40))
        raise AssertionError('conflicting landing accepted')
    except Refused as exc:
        assert exc.code == 'conflict'
"#,
    );
}

fn keeper_fixture(fixture: &str) {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = Command::new("python3")
        .env("PYTHONPATH", repo.join("src"))
        .current_dir(repo.join("tools"))
        .args(["-c", fixture])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
