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
    train = dict(identity, base='a'*40, head='b'*40, parts=[
        {'pr': 5, 'head': 'c'*40, 'ref': 'lane/landed'},
        {'pr': 6, 'head': 'e'*40, 'ref': 'lane/moved'},
    ])
    Store(db).land(principal, dict(train, action='prepare'))
    db.close()

    db = open_db(path, backend='sqlite')
    store = Store(db)
    pending = store.land(principal, dict(repo=identity['repo'], action='pending'))['trains']
    assert len(pending) == 1 and pending[0]['merge'] is None
    def journal(repo, action, **fields):
        return store.land(principal, dict(fields, repo=repo, action=action))
    with (patch.object(landing, 'landing_journal', journal),
          patch.object(landing, 'read_verified_merge', return_value=(None, 'open'))):
        landing.settle_train(pending[0], lease, allow_open=True)
    assert journal(identity['repo'], 'show', pr=17)['train']['merge'] is None
    first = journal(identity['repo'], 'landed', pr=17, merge='d'*40, **lease)
    assert first['open_steps'] == ['evidence', 'close_parts', 'host_sync', 'cleanup']
    assert 'included' not in first
    try:
        journal(identity['repo'], 'advance', pr=17, merge='d'*40, step='evidence', **lease)
    except Refused as exc:
        assert exc.code == 'conflict', exc.code
    else:
        raise AssertionError('settlement advanced without checked part outcomes')
    calls = []
    proof = [dict(host=str(i), head='d'*40, checkout='d'*40, hooks='d'*40, build='d'*40, synced=i==1)
             for i in range(3)]
    def effect(args, **kwargs):
        calls.append(args)
        return Mock(stdout=json.dumps({'verified': proof}) if 'verify-hosts' in args else '', stderr='')
    def live_part(*args):
        assert args[:2] == ('pr', 'view'), args
        return (dict(state='MERGED', headRefOid='c'*40) if args[2] == '5'
                else dict(state='OPEN', headRefOid='f'*40))
    def sweep(state, parts, *, strict):
        assert strict
        assert [part['pr'] for part in parts] == [5]
        assert journal(identity['repo'], 'show', pr=17)['train']['included'] == [5]
        calls.append('cleanup')
    st = dict(merged=train['parts'], head=train['head'])
    with (patch.object(landing, 'landing_journal', journal),
          patch.object(build, 'landing_journal', journal),
          patch.object(build, 'gh_json', side_effect=live_part) as part_views,
          patch.object(build, 'git', return_value=Mock(returncode=0)),
          patch.object(build, 'add_label', return_value=True) as label,
          patch.object(landing, 'read_verified_merge', return_value=('d'*40, 'merged')),
          patch.object(landing, 'run', effect),
          patch.object(landing, 'cleanup_landed', side_effect=sweep),
          patch.object(landing, 'drop_state', side_effect=lambda st: calls.append('drop_state'))):
        landing.settle_train(pending[0], lease, st)
        landing.settle_train(pending[0], lease)
    assert part_views.call_count == 2
    label.assert_called_once_with(identity['repo'], 6, 'needs-update')
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

#[test]
fn keeper_claims_from_accepted_open_train_and_releases_on_drop() {
    keeper_fixture(
        r#"
import json
import sqlite3
import tempfile
from plangraph.keeper import fmt
from plangraph.keeper.auth import Principal, add_principal
from plangraph.keeper.db import open_db
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_types import Result
from plangraph.keeper.store import Store

class Seats:
    def review_seats(self, item, head, decision, **options):
        assert options['require_explicit_accept'] and options['require_diff_head']
        return [{'verdict': 'pass'}] if head in ('b'*40, 'd'*40) else []

class Verifier:
    def verify(self, item, ref, context):
        return Result(True, 'verified', ref)

with tempfile.TemporaryDirectory() as directory:
    path = directory + '/keeper.db'
    db = open_db(path, create=True, backend='sqlite')
    add_principal(db, 'lander', 'router', [])
    add_principal(db, 'writer', 'l1', ['demo'])
    db.close()
    with sqlite3.connect(path) as old:
        old.execute('DROP TABLE IF EXISTS landing_claim_bases')
    db = open_db(path, backend='sqlite')
    store = Store(db, ctx={'spawner_records': Seats()}, verifiers={'decision': Verifier()})
    router = Principal('lander', 'router', [])
    writer = Principal('writer', 'l1', ['demo'])
    store.create_items(router, [
        dict(project='demo', id=key, title='Work ' + key, kind=kind, done_criteria='checked',
             depends_on=['demo:' + dep for dep in deps])
        for key, kind, deps in [
            ('D', 'lane', []), ('C', 'lane', ['D']), ('E', 'lane', ['D', 'C']), ('G', 'goal', ['E'])
        ]
    ])
    repo = 'example/repo'
    lease = store.land(router, dict(repo=repo, action='acquire', owner='fixture'))

    def land(action, pr, **fields):
        return store.land(router, dict(lease, repo=repo, pr=pr, action=action, **fields))

    def ready():
        return {row['key'] for row in store.ready_data(router)['items']}

    def refuses(call):
        try:
            call()
        except Refused:
            return
        raise AssertionError('expected keeper refusal')

    dep = dict(pr=1, head='b'*40, items=['demo:D'])
    child = dict(pr=2, head='d'*40, items=['demo:C'], review_runs={'demo:C': 'run-' + '1'*32})
    land('prepare', 10, base='a'*40, head='c'*40, parts=[dep])
    assert 'demo:C' not in ready()
    refuses(lambda: store.claim(writer, 'demo:C'))
    dep['review_runs'] = {'demo:D': 'run-' + '0'*32}
    refuses(lambda: land('prepare', 10, base='a'*40, head='c'*40,
                         parts=[dict(dep, head='9'*40)]))
    assert 'demo:C' not in ready()
    land('prepare', 10, base='a'*40, head='c'*40, parts=[dep])
    assert 'demo:C' in ready(), 'an accepted open-train dependency must enable ready'
    claim = store.claim(writer, 'demo:C')
    assert claim['base_sha'] == 'c'*40
    assert store.claim_train_base('demo:C') == 'c'*40
    assert 'c'*40 in fmt.result_text('claim', claim)
    land('prepare', 11, base='7'*40, head='8'*40, parts=[child])
    assert 'demo:E' not in ready()
    land('prepare', 11, base='c'*40, head='e'*40, parts=[child])
    assert store.claim(writer, 'demo:E')['base_sha'] == 'e'*40
    refuses(lambda: land('check_dependencies', 11))
    land('prepare', 12, base='a'*40, head='f'*40, parts=[dep, child])
    land('check_dependencies', 12)
    store.add_evidence(writer, 'demo:C', claim['epoch'], 'decision', {'id': 'D-1'})
    store.submit(writer, 'demo:C', claim['epoch'])
    land('abandon', 10)
    assert store.show(router, 'demo:E')['state'] == 'open'
    card = store.show(router, 'demo:C')
    assert card['state'] == 'open' and card['claimed_by'] is None
    release = db.execute("SELECT payload FROM events WHERE key=? AND type='released'",
                         ('demo:C',)).fetchone()
    assert 'abandoned' in json.loads(release['payload'])['reason']
    assert 'demo:C' in ready()
    assert store.claim(writer, 'demo:C')['base_sha'] == 'f'*40
    land('prepare', 12, base='a'*40, head='1'*40, parts=[child])
    assert store.show(router, 'demo:C')['state'] == 'open'
    assert 'demo:C' not in ready()
    refuses(lambda: land('check_dependencies', 12))
    land('prepare', 13, base='a'*40, head='2'*40, parts=[dep])
    assert store.claim(writer, 'demo:C')['base_sha'] == '2'*40
    land('landed', 13, merge='3'*40, included=[1])
    assert store.claim_train_base('demo:C') == '2'*40
    land('check_dependencies', 12)
"#,
    );
}
