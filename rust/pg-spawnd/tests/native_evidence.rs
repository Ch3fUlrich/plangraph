//! Keeper mirrors use Unix ownership checks; exercise the real record reader.
#![cfg(unix)]
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use std::path::Path;

#[test]
fn merged_review_seats_ignore_native_writers_and_origin_receipts() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let out = test_python::command()
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .args(["-c", r#"
import asyncio, json, pathlib, tempfile
from unittest.mock import AsyncMock, patch
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
from plangraph.spawner.remote_ps import native_receipt
from plangraph.spawner.overflow import native_spawn
from plangraph.spawner.service import SpawnService
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    mirror = root / 'mirror'
    def save(host, record):
        path = mirror / host / record['run_id'] / 'record.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        for directory in (path.parent, path.parent.parent): directory.chmod(0o700)
        path.write_text(json.dumps(record))
        path.chmod(0o600)
    review = dict(run_id='run-review', host='target', item_key='project:item', role='review',
                  served_model='gemini-test', status='completed', exit_code=0, attempts=[{}],
                  completion=dict(head_sha='a' * 40, finish_reason='stop'),
                  stdout_tail='TEST_GAMING: no\nVERDICT: PASS\n')
    save('target', review)
    writer = dict(run_id='run-writer', run_kind='native-codex', host='target', role='implement')
    item = dict(key='project:item', state='merged', run_ids=json.dumps(['run-review', 'run-writer']))
    adapter = SpawnerRecordAdapter(mirror)
    # Legacy local writers have no item_key; remote mirrors can have two hosts.
    save('target', writer)
    assert len(adapter.review_seats(item, 'a' * 40, '')) == 1
    receipt = native_receipt(dict(writer, overflow_remote=True))
    assert receipt['origin_receipt'] is True
    save('origin', receipt)
    assert len(adapter.review_seats(item, 'a' * 40, '')) == 1
    (mirror / 'target' / 'run-writer' / 'record.json').unlink()
    (mirror / 'target' / 'run-writer').rmdir()
    assert len(adapter.review_seats(item, 'a' * 40, '')) == 1
    # Review identity and duplicate-host checks still fail closed.
    for host, record in [('origin', review), ('target', dict(review, item_key='project:other'))]:
        save(host, record)
        try: adapter.review_seats(item, 'a' * 40, '')
        except ValueError: pass
        else: raise AssertionError('invalid review witness counted')
        if host == 'origin':
            (mirror / host / 'run-review' / 'record.json').unlink()
            (mirror / host / 'run-review').rmdir()
    async def check_annotation():
        service = SpawnService(state=root / 'state')
        card = dict(run_kind='native-codex', repo=str(root), item='project:item')
        probe = AsyncMock()
        probe.returncode = 0
        probe.communicate.return_value = (b'{"tool_path":"/fixture","resolved_tools":{},"sandbox_ok":true}', b'')
        with patch('plangraph.fleet_snapshot._projection_binary', return_value='/fixture/pg-spawnd'), \
             patch('plangraph.spawner.overflow.lane_admit', side_effect=lambda service, card, prompt: card), \
             patch('asyncio.create_subprocess_exec', side_effect=[probe, OSError('fixture')]):
            record = await native_spawn(service, card, dict(prompt='brief'))
        assert record['item_key'] == 'project:item', record
        assert service.result(record['run_id'])['item_key'] == 'project:item'
    asyncio.run(check_annotation())
"#])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
