#![cfg(unix)]

#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use std::path::Path;

#[test]
fn keeper_backlog_hold_releases_only_the_landed_frozen_part_head() {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let fixture = r#"
from tempfile import TemporaryDirectory
from pathlib import Path
from plangraph.keeper.auth import Principal, add_principal
from plangraph.keeper.db import open_db
from plangraph.keeper.store import Store
from unittest.mock import patch
import merge_batch_build as land

with TemporaryDirectory() as tmp:
    conn = open_db(Path(tmp) / 'stage.db', create=True, backend='sqlite')
    add_principal(conn, 'lander', 'router', [])
    principal = Principal('lander', 'router', [])
    store = Store(conn)
    repo = 'example/repo'
    def journal(repo, action, **fields):
        return store.land(principal, dict(fields, repo=repo, action=action))
    lease = journal(repo, 'acquire', owner='fixture')
    frozen = {'pr': 5, 'head': 'c'*40}
    moved = {'pr': 5, 'head': 'd'*40}
    dropped = {'pr': 6, 'head': 'e'*40}
    journal(repo, 'prepare', pr=17, base='a'*40, head='b'*40, parts=[frozen], **lease)
    with patch.object(land, 'landing_journal', journal):
        assert land.exclude_consumed(repo, [frozen, dropped]) == [frozen, dropped]
        journal(repo, 'landed', pr=17, merge='f'*40, **lease)
        assert land.exclude_consumed(repo, [frozen, dropped]) == [dropped]
        assert land.exclude_consumed(repo, [moved, dropped]) == [moved, dropped]
"#;
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
