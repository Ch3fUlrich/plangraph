"""C2 storage checks use only fixture-owned SQLite keeper files."""
import json
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
    seed(conn)
    for sql in ["UPDATE placement_rule_audit SET actor='other'",
                'DELETE FROM placement_rule_audit',
                "INSERT OR REPLACE INTO placement_rule_audit VALUES (1,'other','{}',NULL,'now')"]:
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            conn.execute(sql)
    conn.close()


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
    for name in ['_LOCKED', 'TRIGGERS', 'ALIASES_DDL', 'RUN_TOKENS_DDL',
                 'OUTBOX_DDL', 'OVERSIGHT_DDL']:
        assert getattr(schema, name) == getattr(schema_guards, name)
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
