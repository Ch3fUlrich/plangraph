use serde_json::{json, Value};
use std::path::Path;

pub fn check(state: &Path, expected: &[(&str, &str, &str, &str)]) {
    let rows: Vec<_> = expected
        .iter()
        .map(|(role, host, harness, exit_class)| {
            json!({"role": role, "host": host, "leg": harness, "harness": harness,
                   "exit_class": exit_class, "task_class": "adhoc"})
        })
        .collect();
    check_rows(state, &rows);
}

pub fn check_rows(state: &Path, expected: &[Value]) {
    let output = crate::test_python::command()
        .arg("-c")
        .arg(
            r#"
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

# SQLite opening does not use the Keeper daemon's singleton lock.
needs_fcntl_placeholder = sys.platform == 'win32' and 'fcntl' not in sys.modules
if needs_fcntl_placeholder:
    sys.modules['fcntl'] = ModuleType('fcntl')
try:
    from plangraph.keeper import auth, db
finally:
    if needs_fcntl_placeholder:
        del sys.modules['fcntl']

from plangraph.keeper.store import Store
from plangraph.spawner.notify import retry_spawn_attempts
from plangraph.spawner.stall_watchdog import read_runs

state = Path(sys.argv[1])
with db.open_db(state / 'attempt-fixture.db', create=True, backend='sqlite') as connection:
    principal = auth.authenticate(connection, auth.add_principal(connection, 'fixture', 'system', []))
    store = Store(connection)
    records = read_runs(state)
    run_ids = {record['run_id'] for record in records}

    class Keeper:
        url = token_file = 'fixture'

        def __init__(self, lose_ack=False):
            self.lose_ack = lose_ack
            self.receipts = []

        def spawn_attempt(self, row):
            receipt = store.record_spawn_attempt(principal, row)
            self.receipts.append(receipt)
            if self.lose_ack:
                raise TimeoutError('fixture lost the acknowledgement after commit')

    def marker(run_id):
        return state / 'runs' / run_id / 'attempt.delivered'

    with patch('plangraph.fleet_snapshot._projection_binary', return_value=sys.argv[2]):
        first = Keeper(lose_ack=True)
        findings = retry_spawn_attempts(state, records + records, keeper=first)
        [receipt] = first.receipts
        lost = receipt['attempt_id']
        assert receipt['duplicate'] is False
        assert findings == [
            dict(rule='spawn-attempt-delivery', run_id=lost, reason='TimeoutError')
        ], findings
        assert not marker(lost).exists()
        assert [row['attempt_id'] for row in store.spawn_attempts(principal, '1d')] == [lost]

        replay = Keeper()
        current = read_runs(state)
        assert retry_spawn_attempts(state, current + current, keeper=replay) == []
        assert len(replay.receipts) == len(run_ids), replay.receipts
        assert {receipt['attempt_id']: receipt['duplicate'] for receipt in replay.receipts} == {
            run_id: run_id == lost for run_id in run_ids
        }, replay.receipts
        assert all(marker(run_id).read_text() == 'delivered\n' for run_id in run_ids)
        replay.receipts.clear()
        assert retry_spawn_attempts(state, read_runs(state), keeper=replay) == []
        assert replay.receipts == []

    rows = store.spawn_attempts(principal, '1d')
    expected = json.loads(sys.argv[3])
    assert len(rows) == len(records) == len(expected), (rows, records)
    assert {row['attempt_id'] for row in rows} == run_ids, rows
    fields = tuple(expected[0])
    def witnesses(entries):
        return sorted(tuple(row[field] for field in fields) for row in entries)
    assert witnesses(rows) == witnesses(expected), rows
"#,
        )
        .arg(state)
        .arg(env!("CARGO_BIN_EXE_pg-spawnd"))
        .arg(serde_json::to_string(expected).unwrap())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
