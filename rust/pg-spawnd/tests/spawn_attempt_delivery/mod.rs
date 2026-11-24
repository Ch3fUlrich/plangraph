use std::path::Path;

// Each test crate includes this module and uses one of the two helpers.
#[allow(dead_code)]
pub fn check(state: &Path, expected: &[(&str, &str, &str, &str)]) {
    let output = crate::test_python::command()
        .arg("-c")
        .arg(
            r#"
import json
import sys
from pathlib import Path
from unittest.mock import patch

from plangraph.keeper import auth, db
from plangraph.keeper.store import Store
from plangraph.spawner.notify import retry_spawn_attempts
from plangraph.spawner.stall_watchdog import read_runs

state = Path(sys.argv[1])
with db.open_db(state / 'attempt-fixture.db', create=True, backend='sqlite') as connection:
    principal = auth.authenticate(connection, auth.add_principal(connection, 'fixture', 'system', []))
    store = Store(connection)
    records = read_runs(state)

    class Keeper:
        url = token_file = 'fixture'

        def __init__(self):
            self.receipts = []

        def spawn_attempt(self, row):
            self.receipts.append(store.record_spawn_attempt(principal, row)['duplicate'])

    with patch('plangraph.fleet_snapshot._projection_binary', return_value=sys.argv[2]):
        first = Keeper()
        assert retry_spawn_attempts(state, records + records, keeper=first) == []
        assert first.receipts == [False] * len(records), first.receipts
        replay = Keeper()
        assert retry_spawn_attempts(state, read_runs(state), keeper=replay) == []
        assert replay.receipts == [], replay.receipts

        # Keeper accepted the POST before the watchdog durably acknowledged it.
        for record in records:
            (state / 'runs' / record['run_id'] / 'attempt.delivered').unlink()
        assert retry_spawn_attempts(state, read_runs(state), keeper=replay) == []
        assert replay.receipts == [True] * len(records), replay.receipts

    rows = store.spawn_attempts(principal, '1d')
    assert {row['attempt_id'] for row in rows} == {record['run_id'] for record in records}, rows
    fields = ('role', 'host', 'leg', 'exit_class')
    actual = sorted([row[field] for field in fields] for row in rows)
    assert actual == sorted(json.loads(sys.argv[3])), actual
    assert all(row['harness'] == row['leg'] for row in rows), rows
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

/// Deliver every terminal run record under `state` to a real Keeper store, twice, and return the
/// persisted rows: one per terminal run, however often delivery repeats.
#[allow(dead_code)]
pub fn delivered(state: &Path) -> Vec<serde_json::Value> {
    let output = crate::test_python::command()
        .arg("-c")
        .arg(
            r#"
import json
import sys
from pathlib import Path
from unittest.mock import patch

from plangraph.keeper import auth, db
from plangraph.keeper.store import Store
from plangraph.spawner.notify import retry_spawn_attempts
from plangraph.spawner.stall_watchdog import read_runs

state = Path(sys.argv[1])
with db.open_db(state / 'attempt-rows.db', create=True, backend='sqlite') as connection:
    principal = auth.authenticate(connection, auth.add_principal(connection, 'fixture', 'system', []))
    store = Store(connection)

    class Keeper:
        url = token_file = 'fixture'

        def spawn_attempt(self, row):
            store.record_spawn_attempt(principal, row)

    with patch('plangraph.fleet_snapshot._projection_binary', return_value=sys.argv[2]):
        for _ in range(2):
            assert retry_spawn_attempts(state, read_runs(state), keeper=Keeper(), limit=1000) == []
    print(json.dumps(store.spawn_attempts(principal, '1d')))
"#,
        )
        .arg(state)
        .arg(env!("CARGO_BIN_EXE_pg-spawnd"))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}
