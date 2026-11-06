"""C3 disposable-schema integration; never accepts a separate live DSN."""
import os
import hashlib
import sqlite3
import uuid

import pytest

from plangraph.keeper import db_pg, schema_pg
from plangraph.keeper import placement_rules_schema as rules

pytestmark = pytest.mark.skipif(not os.environ.get('PG_DSN'), reason='PG_DSN not set')


@pytest.fixture
def pg(monkeypatch):
    import psycopg
    raw = psycopg.connect(os.environ['PG_DSN'], autocommit=True)
    name = 't_rules_' + uuid.uuid4().hex[:12]
    full = schema_pg.MIGRATIONS
    try:
        monkeypatch.setattr(schema_pg, 'MIGRATIONS', full[:-1])
        db_pg.migrate(raw, name)
        monkeypatch.setattr(schema_pg, 'MIGRATIONS', full)
        yield raw, name
    finally:
        raw.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        raw.close()


def upgrade(pg):
    raw, name = pg
    db_pg.migrate(raw, name)
    return raw


def seed(raw):
    raw.execute('INSERT INTO placement_rule_revision VALUES (1,7)')
    raw.execute("INSERT INTO placement_rule_values VALUES ('fleet','','key','9')")
    raw.execute("INSERT INTO placement_rule_audit VALUES (7,'actor','{}',NULL,'now')")


def test_migration_appends_without_rewriting_prior_steps():
    assert schema_pg.MIGRATIONS[-1] == schema_pg.Migration(11, 'placement rules', rules.PG)
    # Shipped _V1 from git blob d9942f65, independently pinned, not an alias.
    assert hashlib.sha256(schema_pg.MIGRATIONS[0].sql.encode()).hexdigest() == (
        'd5b853cc2d7780f2c4305f276db14713aa8fd77d3fc76d6bb20aa6ae2c96e7b0')
    assert [m.version for m in schema_pg.MIGRATIONS] == list(range(1, 12))


def test_pg_sqlite_columns_match(pg):
    raw = upgrade(pg)
    with sqlite3.connect(':memory:') as lite:
        lite.executescript(rules.SQLITE)
        for table in rules.TABLES:
            want = [(r[1], 'bigint' if r[2] == 'INTEGER' else 'text',
                     'NO' if r[3] or r[5] else 'YES',
                     'C' if r[2] == 'TEXT' else None)
                    for r in lite.execute(f'PRAGMA table_info({table})')]
            got = raw.execute('SELECT column_name,data_type,is_nullable,collation_name '
                              'FROM information_schema.columns '
                              'WHERE table_schema=current_schema() AND table_name=%s '
                              'ORDER BY ordinal_position', (table,)).fetchall()
            assert got == want
        for statement in [
            'INSERT INTO placement_rule_revision VALUES (2,0)',
            'INSERT INTO placement_rule_revision VALUES (1,-1)',
            "INSERT INTO placement_rule_values VALUES ('invalid','','k','1')",
            "INSERT INTO placement_rule_values VALUES ('fleet','host','k','1')",
            "INSERT INTO placement_rule_values VALUES ('host','','k','1')",
            "INSERT INTO placement_rule_audit VALUES (0,'a','{}',NULL,'t')",
        ]:
            import psycopg
            with pytest.raises(sqlite3.IntegrityError):
                lite.execute(statement)
            with pytest.raises(psycopg.errors.CheckViolation):
                raw.execute(statement)


def test_keeper_migration_gates_documented():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / 'docs/KEEPER.md').read_text()
    section = text.split('## Placement rules migration-gates', 1)[1]
    for required in ('GO', 'reviewed SHA', 'backup', 'rehearsal', 'migrate role',
                     'runtime', 'restart', 'writers stopped', 'retired_v11'):
        assert required in section


def test_upgrade_idempotent(pg):
    raw, name = pg
    assert db_pg.migrate(raw, name) == [11]
    assert db_pg.migrate(raw, name) == []
    for table in rules.TABLES:
        assert raw.execute(f'SELECT count(*) FROM {table}').fetchone() == (0,)


@pytest.mark.parametrize('statement', [
    'UPDATE placement_rule_audit SET actor=actor',
    'DELETE FROM placement_rule_audit', 'TRUNCATE placement_rule_audit',
    'INSERT INTO placement_rule_revision VALUES (2,0)',
    'UPDATE placement_rule_revision SET revision=-1',
    'UPDATE placement_rule_revision SET revision=9223372036854775808',
    "INSERT INTO placement_rule_values VALUES ('fleet','host','k','1')",
    "INSERT INTO placement_rule_values VALUES ('host','','k','1')",
    "INSERT INTO placement_rule_values VALUES ('invalid','','k','1')",
    "INSERT INTO placement_rule_audit VALUES (0,'a','{}',NULL,'t')",
])
def test_rule_constraints_and_immutable_audit(pg, statement):
    import psycopg
    raw = upgrade(pg)
    seed(raw)
    with pytest.raises(psycopg.Error):
        raw.execute(statement)


def test_runtime_role_cannot_ddl(pg):
    import psycopg
    from psycopg import sql
    raw, name = pg
    upgrade(pg)
    role = 't_rules_role_' + uuid.uuid4().hex[:12]
    ident = sql.Identifier(role)
    raw.execute(sql.SQL('CREATE ROLE {}').format(ident))
    try:
        raw.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(name), ident))
        for table in rules.TABLES:
            raw.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON {} TO {}').format(
                sql.Identifier(table), ident))
        raw.execute(sql.SQL('SET ROLE {}').format(ident))
        seed(raw)
        for statement in ['CREATE TABLE forbidden(x int)',
                          'ALTER TABLE placement_rule_values ADD COLUMN forbidden int']:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                raw.execute(statement)
    finally:
        raw.execute('RESET ROLE')
        raw.execute(sql.SQL('DROP OWNED BY {}').format(ident))
        raw.execute(sql.SQL('DROP ROLE {}').format(ident))


def test_rollback_preserves_rows_and_indexes(pg):
    raw, name = pg
    upgrade(pg)
    seed(raw)
    raw.execute('CREATE INDEX extra_rules_value ON placement_rule_values(value)')
    before = {t: raw.execute(f'SELECT * FROM {t}').fetchall() for t in rules.TABLES}
    versions = raw.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall()
    rules.rollback_pg(raw)
    assert raw.execute("SELECT to_regclass('extra_rules_value_retired_v11')").fetchone()[0]
    for table in rules.TABLES:
        assert raw.execute(f'SELECT * FROM {table}_retired_v11').fetchall() == before[table]
        assert raw.execute('SELECT to_regclass(%s)',
                           (table + '_pkey_retired_v11',)).fetchone()[0]
    assert raw.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall() == versions[:-1]
    with pytest.raises(Exception, match='append-only'):
        raw.execute('TRUNCATE placement_rule_audit_retired_v11')
    with pytest.raises(Exception, match='retired_ledger_exists'):
        db_pg.migrate(raw, name)


def test_repeated_retirement_refused(pg):
    raw = upgrade(pg)
    rules.rollback_pg(raw)
    with pytest.raises(ValueError, match='retired_names_exist'):
        rules.rollback_pg(raw)


def test_failed_migration_atomic(pg, monkeypatch):
    raw, name = pg
    full = schema_pg.MIGRATIONS
    monkeypatch.setattr(schema_pg, 'MIGRATIONS', full[:-1] + (
        schema_pg.Migration(11, 'placement rules', rules.PG + '\nSELECT 1/0;'),))
    with pytest.raises(Exception, match='division by zero'):
        db_pg.migrate(raw, name)
    assert raw.execute('SELECT max(version) FROM schema_migrations').fetchone() == (10,)
    for table in rules.TABLES:
        assert raw.execute('SELECT to_regclass(%s)', (table,)).fetchone() == (None,)


def test_rollback_wrong_schema_refused(pg):
    raw, name = pg
    upgrade(pg)
    from psycopg import sql
    wrong = name + '_wrong'
    raw.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(wrong)))
    try:
        raw.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(wrong)))
        for table in rules.TABLES:
            raw.execute(sql.SQL('CREATE TABLE {} (id integer)').format(sql.Identifier(table)))
        with pytest.raises(ValueError, match='keeper_schema_required'):
            rules.rollback_pg(raw)
        assert raw.execute("SELECT to_regclass('placement_rule_values')").fetchone()[0]
    finally:
        raw.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(name)))
        raw.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(wrong)))
