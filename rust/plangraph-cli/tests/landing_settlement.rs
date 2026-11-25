#![cfg(unix)]

#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use pg_worker_core::executable_test_support::write_script;
use std::path::Path;

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
    def sweep(state, parts, *, strict, writers=None):
        assert strict
        assert [part['pr'] for part in parts] == [5]
        assert journal(identity['repo'], 'show', pr=17)['train']['included'] == [5]
        calls.append('cleanup')
    st = dict(merged=train['parts'], head=train['head'], landing_attempt=dict(
        head=train['head'], mode='train-pr-merge', reruns=['rerun'], go_row='go-row', included=[5]))
    telemetry = Mock()
    with (patch.object(landing, 'landing_journal', journal),
          patch.object(build, 'landing_journal', journal),
          patch.object(build, 'gh_json', side_effect=live_part) as part_views,
          patch.object(build, 'git', return_value=Mock(returncode=0)),
          patch.object(build, 'add_label', return_value=True) as label,
          patch.object(landing, 'write_landing', telemetry),
          patch.object(landing, 'read_verified_merge', return_value=('d'*40, 'merged')),
          patch.object(landing, 'run', effect),
          patch.object(landing, 'cleanup_landed', side_effect=sweep),
          patch.object(landing, 'drop_state', side_effect=lambda st: calls.append('drop_state'))):
        landing.settle_train(pending[0], lease, st)
        landing.settle_train(pending[0], lease)
    assert part_views.call_count == 2
    label.assert_called_once_with(identity['repo'], 6, 'needs-update')
    telemetry.assert_called_once_with(st, 'train-pr-merge', 'd'*40, ['rerun'], 'go-row')
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

#[test]
fn settle_retries_deferred_lane_cleanup() {
    keeper_fixture(
        r#"
from pathlib import Path
import json
from tempfile import TemporaryDirectory
from plangraph.keeper.auth import Principal, add_principal
from plangraph.keeper.db import open_db
from plangraph.keeper.store import Store
from unittest.mock import patch, Mock
import merge_batch_build as build
import merge_batch_land as landing
import merge_batch_util as util
import merge_batch_plan as plan

with TemporaryDirectory() as tmp:
    import os
    os.environ['PG_LANE_STATE'] = tmp + '/lane'
    path = Path(tmp) / 'keeper.db'
    db = open_db(path, create=True, backend='sqlite')
    add_principal(db, 'lander', 'router', [])
    principal = Principal('lander', 'router', [])
    identity = dict(repo='example/repo', pr=17)
    lease = Store(db).land(principal, dict(repo=identity['repo'], action='acquire', owner='test'))
    identity.update(lease)
    train = dict(identity, base='a'*40, head='b'*40, parts=[{'pr': 5, 'head': 'c'*40, 'ref': 'lane/landed-r3'}])
    Store(db).land(principal, dict(train, action='prepare'))
    db.close()
    db = open_db(path, backend='sqlite')
    store = Store(db)
    pending = store.land(principal, dict(repo=identity['repo'], action='pending'))['trains']
    def journal(repo, action, **fields):
        return store.land(principal, dict(fields, repo=repo, action=action))
    journal(identity['repo'], 'landed', pr=17, merge='d'*40, **lease)
    proof = [dict(host=str(i), head='d'*40, checkout='d'*40, hooks='d'*40, build='d'*40, synced=i==1, writers=[])
             for i in range(3)]
    lane_calls, rcs = [], [3, 0]
    def effect(args, **kwargs):
        return Mock(stdout=json.dumps({'verified': proof}) if 'verify-hosts' in args else '', stderr='')
    def cleanup_run(argv, **kwargs):
        if argv[:2] == ['plangraph', 'lane-cleanup']:
            lane_calls.append(argv)
            return Mock(returncode=rcs.pop(0), stdout='lane refused\n', stderr='')
        return Mock(returncode=0, stdout='', stderr='')
    def live_part(*args):
        return dict(state='MERGED', headRefOid='c'*40)
    fork = []
    def head_identity(*args):
        return dict(isCrossRepository=bool(fork), headRefName='lane/landed-r3')
    def settle():
        with (patch.object(landing, 'landing_journal', journal),
              patch.object(build, 'landing_journal', journal),
              patch.object(build, 'gh_json', side_effect=live_part),
              patch.object(build, 'git', return_value=Mock(returncode=0)),
              patch.object(landing, 'write_landing', Mock()),
              patch.object(landing, 'read_verified_merge', return_value=('d'*40, 'merged')),
              patch.object(landing, 'run', effect),
              patch.object(plan, 'gh_json', side_effect=head_identity),
              patch.object(plan, 'run', cleanup_run),
              patch.object(plan, 'git', return_value=Mock(stdout='/repo\n')),
              patch.object(plan, 'worktrees', return_value=[])):
            landing.settle_train(pending[0], lease)
    try:
        settle()
    except util.MergeBatchError as exc:
        assert 'deferred' in str(exc), exc
    else:
        raise AssertionError('a refused lane delete must hold the cleanup step open')
    assert 'cleanup' in journal(identity['repo'], 'landed', pr=17, merge='d'*40, **lease)['open_steps']
    settle()
    assert journal(identity['repo'], 'landed', pr=17, merge='d'*40, **lease)['open_steps'] == []
    assert len(lane_calls) == 2 and lane_calls[0] == lane_calls[1], lane_calls
    argv = lane_calls[0]
    assert argv[argv.index('--refs') + 1] == 'refs/heads/lane/landed-r3:' + 'c'*40, argv
    assert '--stems' not in argv and '--apply' in argv, argv
    assert argv[argv.index('--gh-repo') + 1] == 'example/repo', argv
    # A part whose head lives in a fork reaches neither the legacy sweeper nor the lane cleanup.
    fork.append(True)
    lane_calls.clear()
    sweeps = []
    def sweep_run(argv, **kwargs):
        if argv[0] == 'pg-worktree-sweep':
            sweeps.append(argv)
        if argv[:2] == ['plangraph', 'lane-cleanup']:
            lane_calls.append(argv)
        return Mock(returncode=0, stdout='', stderr='')
    forked = dict(repo='example/repo', worktree='', branch='', head='b'*40, prs={'5': dict(ref='lane/landed-r3')})
    part = [dict(pr=5, head='c'*40)]
    with (patch.object(plan, 'gh_json', side_effect=head_identity), patch.object(plan, 'run', sweep_run),
          patch.object(plan, 'git', return_value=Mock(stdout='/repo\n')), patch.object(plan, 'worktrees', return_value=[])):
        plan.cleanup_landed(forked, part, strict=True, writers=True)   # fork: local-only sweep, no verb
        fork.clear()
        plan.cleanup_landed(forked, part, strict=False, writers=True)  # same repo, writers stopped
        plan.cleanup_landed(forked, part, strict=False, writers=False) # writer-stop not recorded: held
        plan.cleanup_landed(forked, part, strict=False)                # no host verification: no verb
    assert '--delete-remote' not in sweeps[0] and '--remote-target' not in sweeps[0], sweeps[0]
    assert '--delete-remote' in sweeps[1] and sweeps[1][sweeps[1].index('--remote-target') + 1] == 'lane/landed-r3:' + 'c'*40, sweeps[1]
    assert '--delete-remote' not in sweeps[2] and '--remote-target' not in sweeps[2], sweeps[2]
    assert '--delete-remote' not in sweeps[3] and '--remote-target' not in sweeps[3], sweeps[3]  # legacy: no evidence
    assert len(lane_calls) == 1, lane_calls
    # Only a host verification with a writers report and no kept writer counts as writer-stop evidence.
    stop = plan.writers_stopped
    assert stop([dict(writers=[])]) and stop([dict(writers=[dict(status='stopped')])])
    assert not stop(None) and not stop([]) and not stop([dict(writers=None)]) and not stop([dict()])
    assert not stop([dict(writers=[dict(status='kept')])]) and not stop([dict(writers=[]), dict()])
"#,
    );
}

#[path = "lane_support/mod.rs"]
mod lane_support;

/// The settlement cleanup (`cleanup_landed`, the keeper settle's cleanup step) against a real hooked fixture remote: the hook refuses a remote that is not
/// the App's, the step fails, and the retry sends the same frozen ref and SHA and deletes it.
#[test]
fn settle_retries_a_real_hook_refusal_with_the_same_frozen_sha() {
    let f = lane_support::Fixture::new();
    let sha = f.lane("lane/landed", "main", "m.txt", "m\n");
    f.main_ff("lane/landed");
    f.set(
        "pr-9.json",
        r#"{"isCrossRepository":false,"headRefName":"lane/landed"}"#,
    );
    f.hook_on(false);
    // The legacy sweeper is not under test here: a stub that succeeds stands in for it.
    write_script(&f.bin, "pg-worktree-sweep", "#!/bin/sh\nexit 0\n");
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let binary = Path::new(env!("CARGO_BIN_EXE_plangraph"));
    let step = || {
        let script = r#"
import json, os, sys
sys.path.insert(0, os.environ['TOOLS'])
import merge_batch_plan as plan
calls = []
real = plan.run
def recording(argv, **kwargs):
    calls.append(list(argv))
    return real(argv, **kwargs)
plan.run = recording
st = dict(repo='fixture/origin', worktree='', branch='', head='b'*40, prs={'9': dict(ref='lane/landed')})
try:
    plan.cleanup_landed(st, [dict(pr=9, head=os.environ['FROZEN'])], strict=True, writers=True)
    outcome = 'done'
except plan.MergeBatchError as exc:
    outcome = 'deferred' if 'deferred' in str(exc) else 'other: ' + str(exc)
print(json.dumps([outcome, calls]))
"#;
        let path = format!(
            "{}:{}:/usr/bin:/bin",
            f.bin.display(),
            binary.parent().unwrap().display()
        );
        let output = test_python::command()
            .current_dir(&f.work)
            .env("PYTHONPATH", repo.join("src"))
            .env("TOOLS", repo.join("tools"))
            .env("PATH", path)
            .env("FAKE_GH_DIR", &f.gh)
            .env("FROZEN", &sha)
            .env("WORK", &f.work)
            .env("PG_LANE_STATE", &f.state)
            .env("PG_LANE_GH_HOST", "local")
            .env(
                "PG_SWEEP_CLAIMS_READER",
                format!("cat {}", f.gh.join("claims.json").display()),
            )
            .args(["-c", script])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap()
    };
    let first = step();
    assert!(first.starts_with(r#"["deferred""#), "{first}");
    assert_eq!(f.heads(), ["landed"], "a refused delete leaves the ref");
    // The operator marks the App's remote; the retry carries the same frozen pair.
    lane_support::git(
        &f.work,
        &["config", "remote.origin.plangraph-lane-delete", "app"],
    );
    let second = step();
    assert!(second.starts_with(r#"["done""#), "{second}");
    assert!(f.heads().is_empty());
    let frozen = format!("refs/heads/lane/landed:{sha}");
    assert!(
        first.contains(&frozen) && second.contains(&frozen),
        "{first} {second}"
    );
}

fn keeper_fixture(fixture: &str) {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
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
        assert options == dict(allow_cli_reported=True, require_diff_head=True, require_explicit_accept=True)
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
        old.execute('PRAGMA user_version=21')
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

#[test]
fn landing_commands_publish_current_reviews_and_abandon_unmerged_train() {
    keeper_fixture(
        r#"
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
import merge_batch_build as build
import merge_batch_land as land
import merge_batch_util as util

with TemporaryDirectory() as directory, ExitStack() as stack:
    path = Path(directory)
    seat = 'run-' + '1'*32
    part = dict(pr=2, head='b'*40, items=['demo:D'])
    state = dict(name='train', repo='example/repo', branch='batch/train', base='main',
                 base_head='a'*40, worktree=directory, status='built',
                 merged=[part], prs={'2': {'ref': 'lane/dependency'}})
    journal = Mock(side_effect=lambda repo, action, **fields: {
        'acquire': {'owner': 'test', 'token': 42}, 'show': {'train': {'merge': None}}
    }.get(action, {}))
    for module in (build, land):
        for name, value in dict(landing_journal=journal, keeper_landing_configured=lambda: True,
                                load_state=lambda name: state).items():
            stack.enter_context(patch.object(module, name, value))
    stack.enter_context(patch.object(util, 'git', return_value=Mock(stdout='')))
    for name, value in dict(git=Mock(side_effect=[Mock(stdout=''), Mock(stdout='c'*40), Mock()]),
                            gh=Mock(return_value=Mock(stdout='https://example.org/example/repo/pull/17')),
                            adopt_open_pr=Mock(), render_body=Mock(return_value='body'),
                            record_stage=Mock(), save_state=Mock(), part_items=Mock(return_value={'demo:D'}),
                            part_seat=Mock()).items():
        stack.enter_context(patch.object(land, name, value))
    stack.enter_context(patch.dict(land.os.environ))
    stack.enter_context(patch.object(land, 'part_review',
                                    return_value={'verdict': 'REQUEST_CHANGES', 'head': part['head']}))

    def prepare_calls():
        return [call.kwargs for call in journal.call_args_list if call.args[1] == 'prepare']

    assert land.cmd_push(SimpleNamespace(name='train', ci_run=None)) == 0
    assert len(prepare_calls()) == 1
    pushed = prepare_calls()[0]
    assert (pushed['pr'], pushed['base'], pushed['head'], pushed['token']) == (17, 'a'*40, 'c'*40, 42)
    assert pushed['parts'] == [dict(part, ref='lane/dependency')]
    assert land.cmd_review(SimpleNamespace(name='train', pr=2, file=None, run_id=seat)) == 0
    assert len(prepare_calls()) == 2
    assert prepare_calls()[-1]['parts'][0]['review_runs'] == {'demo:D': seat}
    assert land.cmd_review(SimpleNamespace(name='train', pr=2, file='review.txt')) == 0
    assert len(prepare_calls()) == 3
    assert 'review_runs' not in prepare_calls()[-1]['parts'][0]
    assert part['review_runs'] == {'demo:D': seat}

    state['worktree'] = str(path / 'removed')
    for name, value in dict(state_path=lambda name: path, state_dir=lambda: path,
                            worktrees=lambda: [], git=Mock(), drop_state=Mock()).items():
        stack.enter_context(patch.object(build, name, value))
    stack.enter_context(patch.object(build.os, 'chdir'))
    assert build.cmd_abort(SimpleNamespace(name='train')) == 0
    assert [(call.args[1], call.kwargs.get('pr')) for call in journal.call_args_list][-4:] == [
        ('acquire', None), ('show', 17), ('abandon', 17), ('release', None)]
"#,
    );
}

#[test]
fn land_with_lease_checks_dependencies_before_host_merge_and_honors_refusal() {
    keeper_fixture(
        r#"
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch
import merge_batch_land as land
import merge_batch_util as util

repo, base, head = 'example/repo', 'a'*40, 'b'*40
state = dict(repo=repo, status='pushed', pr=17, name='train', base_head=base, head=head,
             branch='batch/train', worktree='fixture', prs={'5': {'ref': 'lane/part'}},
             merged=[dict(pr=5, head='c'*40, merge='d'*40, items=['example:PART'])])
args = SimpleNamespace(batch_pr=17, approval='approval.txt', checks='', go_row=None)
lease = {'token': 42, 'owner': 'test'}

for refused in (False, True):
    calls = []
    def journal(repo_arg, action, **fields):
        assert repo_arg == repo and action == 'check_dependencies'
        assert fields == dict(lease, pr=17, head=head)
        calls.append('dependency check')
        if refused:
            raise land.MergeBatchError('demo:C cannot land before demo:D')

    def gh(*args, **kwargs):
        assert args[:3] == ('pr', 'merge', '17')
        calls.append('host merge')
        raise land.MergeBatchError('host merge boundary')

    def hold(refs):
        assert refs == ['lane/part']
        calls.append('hold lane')

    with ExitStack() as stack:
        stack.enter_context(patch.object(util, 'git', return_value=Mock(stdout='')))
        for name in ('check_approval', 'require_part_seats', 'require_current_base', 'save_state',
                     'require_green', 'record_reruns', 'prepare_train', 'require_main_green', 'require_migration_go',
                     'merge_trailers'):
            stack.enter_context(patch.object(land, name, Mock()))
        stack.enter_context(patch.object(land, 'require_full_run', return_value=[]))
        stack.enter_context(patch.object(land, 'landing_journal', journal))
        stack.enter_context(patch.object(land, 'gh', gh))
        stack.enter_context(patch.object(land, 'hold_lane_refs', hold))
        stack.enter_context(patch.object(land, 'gh_json', side_effect=[
            dict(state='OPEN', headRefOid=head, isDraft=False, baseRefName='main'), {'object': {'sha': base}}
        ]))
        try:
            land.land_with_lease(args, lease, state, "train-pr-merge")
        except land.MergeBatchError as exc:
            assert str(exc) == ('demo:C cannot land before demo:D' if refused else 'host merge boundary')
        else:
            raise AssertionError('expected the dependency refusal or simulated host boundary')
    assert calls == ['hold lane', 'dependency check'] + ([] if refused else ['host merge'])
"#,
    );
}
