//! Exercise the existing Python keeper until its storage port lands.
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use std::path::Path;

fn keeper_case(case: &str) {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let output = test_python::command()
        .current_dir(root)
        .env("PYTHONPATH", root.join("src"))
        .args(["-c", CASE, case])
        .output()
        .expect("run keeper regression");
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn keeper_priority_refuses_out_of_range_without_mutation() {
    keeper_case("refuse");
}

#[test]
fn keeper_priority_accepts_scale_and_preserves_oversight_threshold() {
    keeper_case("accept");
}

const CASE: &str = r#"
import sys
import tempfile
from pathlib import Path
from plangraph.keeper import auth, db
from plangraph.keeper.errors import Refused
from plangraph.keeper.oversight_operator import r8_operator
from plangraph.keeper.store import Store

with tempfile.TemporaryDirectory() as directory:
    conn = db.open_db(Path(directory) / 'keeper.db', create=True, backend='sqlite')
    token = auth.add_principal(conn, 'router', 'router', [])
    p = auth.authenticate(conn, token)
    clock = [1_700_000_000.0]
    s = Store(conn, clock=lambda: clock[0])
    def item(name, **fields):
        return {'project': 'demo', 'id': name, 'kind': 'goal', 'title': name,
                'description': 'Why: test bounds\nHow: validate priority',
                'track': 'T0', 'owner': 'tester', 'estimate_h': 1,
                'done_criteria': 'criteria', 'evidence_kinds': ['any'], **fields}
    s.create_items(p, [item('A')])
    assert s._item('demo:A')['priority'] == 0
    def snapshot():
        return ([tuple(r) for r in conn.execute('SELECT * FROM items ORDER BY key')],
                [tuple(r) for r in conn.execute('SELECT * FROM events ORDER BY seq')])
    if sys.argv[1] == 'refuse':
        failures = []
        for n in (-1, 11, True, 1.5, '9'):
            operations = {
                'define': lambda: s.create_items(p, [item('NEW', priority=n)]),
                'import': lambda: s.import_items(p, [item('IMPORTED', priority=n)]),
                'priority': lambda: s.set_priority(p, 'demo:A', n),
                'edit': lambda: s.edit_definition(p, 'demo:A', {'title': 'changed', 'priority': n}),
                'batch priority': lambda: s.apply_ops(p, [
                    {'op': 'priority', 'key': 'demo:A', 'priority': 4},
                    {'op': 'priority', 'key': 'demo:A', 'priority': n}]),
                'batch create': lambda: s.apply_ops(p, [
                    {'op': 'create', 'item': item('NEW', priority=n)}]),
                'batch edit': lambda: s.apply_ops(p, [
                    {'op': 'edit', 'key': 'demo:A', 'fields': {'priority': n}}]),
            }
            for name, operation in operations.items():
                before = snapshot()
                try:
                    operation()
                except Refused as exc:
                    if exc.code != 'validation':
                        failures.append(f'{name} {n!r}: {exc.code}')
                else:
                    failures.append(f'{name} accepted {n!r}')
                if snapshot() != before:
                    failures.append(f'{name} {n!r} mutated items/events')
        assert not failures, '\n'.join(failures)
    else:
        for n in range(11):
            s.create_items(p, [item(f'P{n}', priority=n)])
            assert s._item(f'demo:P{n}')['priority'] == n
            s.set_priority(p, 'demo:A', n)
            assert s._item('demo:A')['priority'] == n
            s.apply_ops(p, [{'op': 'priority', 'key': 'demo:A', 'priority': n}])
            assert s._item('demo:A')['priority'] == n
        assert s._item('demo:P0')['priority'] == 0
        assert s._item('demo:A')['priority'] == 10
        s.create_items(p, [item('OP', kind='operator_task'),
                           item('GATE', depends_on=['demo:OP'], priority=8)])
        clock[0] += 86401
        for n, expected in ((8, False), (9, True), (10, True)):
            s.set_priority(p, 'demo:GATE', n)
            findings = r8_operator(conn, clock[0])
            assert not any(f['kind'] == 'oversight_check_failed' for f in findings), findings
            assert any(f['kind'] == 'operator_step_stale' for f in findings) == expected, findings
    conn.close()
"#;
