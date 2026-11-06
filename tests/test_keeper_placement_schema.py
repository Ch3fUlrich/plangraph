"""C2 storage checks use only fixture-owned SQLite keeper files."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3

import pytest

from plangraph.keeper import db, schema, schema_guards
from plangraph.keeper.placement_rules_schema import SQLITE


def shape(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE name LIKE '%placement_rule%' ORDER BY name")]


def test_fresh_and_upgrade_same_rules_tables(tmp_path):
    fresh = db.open_db(tmp_path / 'fresh.db', create=True)
    path = tmp_path / 'old.db'
    raw = sqlite3.connect(path, isolation_level=None)
    raw.executescript(schema.SCHEMA.replace(SQLITE, ''))
    assert not shape(raw)
    assert db._upgrade(raw) is None
    raw.close()
    upgraded = db.open_db(path)
    assert shape(fresh) == shape(upgraded)
    assert schema.SCHEMA_VERSION == 17
    fresh.close()
    upgraded.close()


def seed(conn):
    edit = json.dumps(dict(scope='Fleet', key='worker_cap', value=4,
                           expected_revision=0, rollback_of=None),
                      sort_keys=True, separators=(',', ':'))
    conn.execute('INSERT INTO placement_rule_revision VALUES (1,1)')
    conn.execute("INSERT INTO placement_rule_values VALUES ('fleet','','worker_cap','4')")
    conn.execute('INSERT INTO placement_rule_audit VALUES (1,?,?,NULL,?)',
                 ('operator', edit, '2026-01-01T00:00:00Z'))
    return edit


def test_repeated_upgrade_preserves_rows(tmp_path):
    path = tmp_path / 'keeper.db'
    conn = db.open_db(path, create=True)
    edit = seed(conn)
    conn.close()
    for _ in range(3):
        conn = db.open_db(path)
        assert tuple(conn.execute('SELECT * FROM placement_rule_revision').fetchone()) == (1, 1)
        assert tuple(conn.execute('SELECT * FROM placement_rule_values').fetchone()) == (
            'fleet', '', 'worker_cap', '4')
        assert tuple(conn.execute('SELECT * FROM placement_rule_audit').fetchone()) == (
            1, 'operator', edit, None, '2026-01-01T00:00:00Z')
        conn.close()


def test_revision_and_scope_constraints(tmp_path):
    conn = db.open_db(tmp_path / 'keeper.db', create=True)
    for values in [(0, 0), (2, 0), (1, -1), (1, None)]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO placement_rule_revision VALUES (?,?)', values)
    conn.execute('INSERT INTO placement_rule_revision VALUES (1,0)')
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO placement_rule_revision VALUES (1,1)')
    for scope, host in [('fleet', 'node'), ('host', ''), ('other', ''), (None, ''), ('host', None)]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO placement_rule_values VALUES (?,?,?,?)',
                         (scope, host, 'worker_cap', '4'))
    for scope, host in [('fleet', ''), ('host', 'node')]:
        conn.execute('INSERT INTO placement_rule_values VALUES (?,?,?,?)',
                     (scope, host, 'worker_cap', '4'))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO placement_rule_values VALUES (?,?,?,?)',
                         (scope, host, 'worker_cap', '5'))
    for revision in [0, -1]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO placement_rule_audit VALUES (?,'operator','{}',NULL,'now')",
                         (revision,))
    conn.close()


def test_audit_update_delete_refused(tmp_path):
    conn = db.open_db(tmp_path / 'keeper.db', create=True)
    # REPLACE's implicit DELETE only fires guards with recursive triggers on.
    assert conn.execute('PRAGMA recursive_triggers').fetchone()[0] == 1
    seed(conn)
    for sql in ["UPDATE placement_rule_audit SET actor='other'",
                'DELETE FROM placement_rule_audit',
                "INSERT OR REPLACE INTO placement_rule_audit VALUES (1,'other','{}',NULL,'now')"]:
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            conn.execute(sql)
    conn.close()


def test_existing_audit_replace_refused(tmp_path):
    conn = db.open_db(tmp_path / 'keeper.db', create=True)
    conn.execute("INSERT INTO audit_log(id,action) VALUES (1,'original')")
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        conn.execute("INSERT OR REPLACE INTO audit_log(id,action) VALUES (1,'other')")
    assert conn.execute('SELECT action FROM audit_log').fetchone()[0] == 'original'
    conn.close()


def test_migration_gates_under_c2():
    doc = (Path(__file__).resolve().parents[1] / 'docs/placement-rules.md').read_text()
    parent = None
    for line in doc.splitlines():
        if line.startswith('## '):
            parent = line
        if line.startswith('### Migration-gates:'):
            assert parent == '## C2 existing-keeper SQLite storage (D5)'
            break
    else:
        pytest.fail('missing C2 migration gates')


def test_runtime_ddl_denied(tmp_path):
    conn = db.open_db(tmp_path / 'keeper.db', create=True)
    for sql in ['DROP TABLE placement_rule_values',
                'DROP TRIGGER trg_placement_rule_audit_no_delete',
                'ALTER TABLE placement_rule_audit ADD COLUMN extra TEXT',
                'CREATE TABLE another_store (id INTEGER)']:
        with pytest.raises(sqlite3.DatabaseError, match='authorized'):
            conn.execute(sql)
    conn.close()


def test_no_seed_override_rows(tmp_path):
    path = tmp_path / 'keeper.db'
    for create in [True, False]:
        conn = db.open_db(path, create=create)
        for table in ['placement_rule_revision', 'placement_rule_values', 'placement_rule_audit']:
            assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
        conn.close()
    assert {p.name for p in tmp_path.iterdir()} == {'keeper.db'}


def test_extraction_preserves_existing_guards(tmp_path):
    conn = db.open_db(tmp_path / 'keeper.db', create=True)
    # SHA-256 of exact DDL from merge-base c2977272 schema.py, not re-exports.
    expected = {
        '_LOCKED': '82563ecbd4111d8b37607217e7463bde7ea85278e278070ea3af112adf46d1e5',
        'TRIGGERS': '65a437273ed46a6205f8baa93166a87c9aa52756094c17724ca359b64bacd879',
        'ALIASES_DDL': 'cefad11f48b7eb7bbfb1982c07e379143e8592a68ce195a1867a23041d9704f1',
        'RUN_TOKENS_DDL': ''.join([
            '372c6f7e', '30a179ca', 'ef8a59c0', '1e4e02a5',
            'a501585b', 'defb1be3', 'f51877b7', '9835af18',
        ]),
        'OUTBOX_DDL': '84caf66f30f7d5fe86f873847b21d2bba8933660bd3bd2efac4cc56583a54b0a',
        'OVERSIGHT_DDL': 'cbec2e2839f8407370c27060933d5ed09b08e311064c1142d3036b41dbefd9d9',
    }
    for name, digest in expected.items():
        for module in (schema, schema_guards):
            assert hashlib.sha256(getattr(module, name).encode()).hexdigest() == digest
    locked = list(db._LOCK_TRIGGER.finditer(schema.TRIGGERS))
    assert len(locked) == 10
    for match in locked:
        assert "'answered'" in match.group(0)
        assert conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',
                            (match.group(1),)).fetchone()
    for ddl in [schema.TRIGGERS, schema.ALIASES_DDL, schema.OUTBOX_DDL, schema.OVERSIGHT_DDL]:
        for name in re.findall(r'CREATE TRIGGER (?:IF NOT EXISTS )?(\w+)', ddl):
            assert conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone()
    conn.execute("INSERT INTO items(key,project,id,state) VALUES ('demo:A','demo','A','cancelled')")
    for sql in ["UPDATE items SET title='changed' WHERE key='demo:A'",
                "INSERT INTO evidence(key,kind) VALUES ('demo:A','commit')",
                "INSERT INTO edges(src,dst) VALUES ('demo:A','demo:B')"]:
        with pytest.raises(sqlite3.IntegrityError, match='locked'):
            conn.execute(sql)
    conn.close()
