#![cfg(unix)]

use std::{path::Path, process::Command};

#[test]
fn keeper_reopens_the_exact_landed_train_and_advances_each_step_once() {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let fixture = r#"
from pathlib import Path
from tempfile import TemporaryDirectory
from plangraph.keeper.auth import Principal, add_principal
from plangraph.keeper.db import open_db
from plangraph.keeper.store import Store
from plangraph.keeper.errors import Refused
from unittest.mock import patch, Mock
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
    first = Store(db).land(principal, dict(identity, action='landed', merge='d'*40))
    assert first['open_steps'] == ['evidence', 'close_parts', 'host_sync', 'cleanup']
    calls = []
    def effect(args, **kwargs):
        calls.append(args)
        return Mock(stdout='', stderr='')
    st = {'merged': []}
    with (patch.object(landing, 'landing_journal', journal),
          patch.object(landing, 'read_verified_merge', return_value=('d'*40, 'merged')),
          patch.object(landing, 'run', effect),
          patch.object(landing, 'cleanup_landed', side_effect=lambda *args, **kw: calls.append('cleanup')),
          patch.object(landing, 'drop_state', side_effect=lambda st: calls.append('drop_state')),
          patch.dict('os.environ', {'PG_LANDING_HOST_VERIFY': 'verify'})):
        landing.settle_train(pending[0], lease, st)
        landing.settle_train(pending[0], lease, st)
    assert calls == [['plangraph', 'merge-close'], ['verify', 'example/repo', 'd'*40],
                     'cleanup', 'drop_state'], calls
    assert store.land(principal, dict(repo=identity['repo'], action='pending')) == {'trains': []}
    assert store.land(principal, dict(identity, action='landed', merge='d'*40))['open_steps'] == []
    try:
        store.land(principal, dict(identity, action='landed', merge='e'*40))
        raise AssertionError('conflicting landing accepted')
    except Refused as exc:
        assert exc.code == 'conflict'
"#;
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
