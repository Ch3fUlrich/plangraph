"""Hermetic per-test schemas, using the keeper PG tests' PG_DSN convention."""
import os
import uuid

import pytest

from plangraph.keeper import db_pg, schema_pg
from plangraph.keeper.schema_pg_spawner import TABLES, SpawnerRollbackRefused, rollback

pytestmark = pytest.mark.skipif(not os.environ.get('PG_DSN'), reason='PG_DSN not set')
LIVE = ('claimed', 'running', 'unknown', 'cancelling')
TERMINAL = ('completed', 'failed', 'cancelled', 'withdrawn')
STATES = ('queued',) + LIVE + TERMINAL
PUBLICATION = ('not_started', 'pending', 'publishing', 'published', 'finalize_conflict')


@pytest.fixture
def pg(monkeypatch):
    import psycopg

    raw = psycopg.connect(os.environ['PG_DSN'], autocommit=True)
    name = 't_spawner_' + uuid.uuid4().hex[:12]
    full = schema_pg.MIGRATIONS
    try:
        monkeypatch.setattr(schema_pg, 'MIGRATIONS', full[:9])
        assert db_pg.migrate(raw, name) == list(range(1, 10))
        raw.execute("INSERT INTO principals(name,role) VALUES ('retained','reader')")
        monkeypatch.setattr(schema_pg, 'MIGRATIONS', full)
        yield raw, name
    finally:
        raw.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        raw.close()


def upgrade(pg):
    raw, name = pg
    db_pg.migrate(raw, name)
    return raw


def job(raw, status='queued', run='run', n=1):
    raw.execute('INSERT INTO spawner_jobs'
                '(run_id,attempt_no,attempt_id,target_host,payload_hash,status) '
                'VALUES (%s,%s,%s,%s,%s,%s)', (run, n, f'{run}-{n}', 'target', 'hash', status))


def test_upgrade(pg):
    raw, name = pg
    assert schema_pg.LATEST == 12
    assert db_pg.migrate(raw, name) == [10, 11, 12]
    assert db_pg.migrate(raw, name) == []
    assert raw.execute("SELECT role FROM principals WHERE name='retained'").fetchone() == ('reader',)
    assert {r[0] for r in raw.execute("SELECT tablename FROM pg_tables WHERE "
                                    "schemaname=current_schema() AND tablename LIKE 'spawner_%'")} == set(TABLES)
    assert raw.execute("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
                       "AND indexname='idx_spawner_jobs_target_status'").fetchone()[0].endswith(
                           '(target_host, status)')


@pytest.mark.parametrize('state', STATES + ('invalid',))
def test_execution_states(pg, state):
    raw = upgrade(pg)
    if state == 'invalid':
        with pytest.raises(Exception, match='check constraint'):
            job(raw, state)
    else:
        job(raw, state)


@pytest.mark.parametrize('state', PUBLICATION + ('invalid',))
def test_publication_states(pg, state):
    raw = upgrade(pg)
    job(raw)
    sql = "INSERT INTO spawner_finalizations(run_id,finalizer_id,publication_state) VALUES ('run','f',%s)"
    if state == 'invalid':
        with pytest.raises(Exception, match='check constraint'):
            raw.execute(sql, (state,))
    else:
        raw.execute(sql, (state,))


@pytest.mark.parametrize('first', LIVE)
@pytest.mark.parametrize('second', LIVE)
def test_live_pairs(pg, first, second):
    raw = upgrade(pg)
    job(raw, first)
    with pytest.raises(Exception, match='idx_spawner_jobs_live'):
        job(raw, second, n=2)
    for n, status in enumerate(TERMINAL * 2, 3):
        job(raw, status, n=n)


@pytest.mark.parametrize('table,column,value', [
    ('spawner_jobs', 'attempt_no', 0), ('spawner_jobs', 'generation', -1),
    ('spawner_finalizations', 'generation', -1),
    ('spawner_blobs', 'total_size', -1), ('spawner_blobs', 'total_size', 67108865),
    ('spawner_blobs', 'chunks', [b'x' * 4194305]),
    ('spawner_blobs', 'chunks', [None]), ('spawner_blobs', 'chunks', [b'x']),
])
def test_checks(pg, table, column, value):
    raw = upgrade(pg)
    job(raw)
    raw.execute("INSERT INTO spawner_finalizations(run_id,finalizer_id) VALUES ('run','f')")
    raw.execute("INSERT INTO spawner_blobs(blob_id) VALUES ('blob')")
    with pytest.raises(Exception, match='check constraint'):
        if column == 'total_size' and value > 0:
            raw.execute('UPDATE spawner_blobs SET total_size=%s, chunks=%s',
                        (67108865, [b'x' * 4194304] * 16 + [b'x']))
        elif column == 'chunks' and value[0] and len(value[0]) > 4194304:
            # total_size matches the chunk sum, so only the per-chunk limit can refuse this.
            raw.execute('UPDATE spawner_blobs SET total_size=%s, chunks=%s', (len(value[0]), value))
        else:
            raw.execute(f'UPDATE {table} SET {column}=%s', (value,))


def test_blob_boundaries(pg):
    raw = upgrade(pg)
    chunks = [b'x' * 4194304] * 16
    raw.execute('INSERT INTO spawner_blobs(blob_id,chunks,total_size) VALUES (%s,%s,%s)',
                ('blob', chunks, 67108864))


def test_blob_rejects_two_dimensions(pg):
    raw = upgrade(pg)
    with pytest.raises(Exception, match='check constraint'):
        raw.execute('INSERT INTO spawner_blobs(blob_id,chunks,total_size) VALUES (%s,%s,%s)',
                    ('matrix', [[b'x', b'y'], [b'z', b'w']], 4))


@pytest.mark.parametrize('column', ['attempt_no', 'attempt_id'])
def test_attempt_uniqueness(pg, column):
    raw = upgrade(pg)
    job(raw)
    job(raw, run='other')
    value = 1 if column == 'attempt_no' else 'run-1'
    if column == 'attempt_no':
        job(raw, n=2)
    with pytest.raises(Exception, match='unique constraint'):
        raw.execute(f'UPDATE spawner_jobs SET {column}=%s', (value,))


def test_run_trigger(pg):
    raw = upgrade(pg)
    with pytest.raises(Exception, match='spawner_unknown_run'):
        raw.execute("INSERT INTO spawner_finalizations(run_id,finalizer_id) VALUES ('run','f')")
    job(raw)
    raw.execute("INSERT INTO spawner_finalizations(run_id,finalizer_id) VALUES ('run','f')")
    with pytest.raises(Exception, match='spawner_unknown_run'):
        raw.execute("UPDATE spawner_finalizations SET run_id='missing'")


@pytest.mark.parametrize('table,column', [('spawner_jobs', 'result_blob_id'),
    ('spawner_jobs', 'salvage_blob_id'), ('spawner_finalizations', 'result_blob_id')])
def test_blob_references(pg, table, column):
    raw = upgrade(pg)
    job(raw)
    raw.execute("INSERT INTO spawner_finalizations(run_id,finalizer_id) VALUES ('run','f')")
    with pytest.raises(Exception, match='foreign key constraint'):
        raw.execute(f"UPDATE {table} SET {column}='missing'")
    raw.execute("INSERT INTO spawner_blobs(blob_id) VALUES ('blob')")
    raw.execute(f"UPDATE {table} SET {column}='blob'")


@pytest.mark.parametrize('state', ('queued',) + LIVE)
def test_rollback_refuses(pg, state):
    raw = upgrade(pg)
    job(raw, state)
    with pytest.raises(SpawnerRollbackRefused, match='spawner_non_terminal_jobs'):
        rollback(raw)
    assert raw.execute('SELECT count(*) FROM spawner_jobs').fetchone()[0] == 1


def test_rollback_preserves(pg):
    raw, name = pg
    upgrade(pg)
    for n, status in enumerate(TERMINAL, 1):
        job(raw, status, n=n)
    raw.execute("INSERT INTO spawner_hosts(host_id) VALUES ('target')")
    raw.execute("INSERT INTO spawner_blobs(blob_id) VALUES ('blob')")
    raw.execute("INSERT INTO spawner_finalizations(run_id,finalizer_id,result_blob_id) "
                "VALUES ('run','f','blob')")
    before = {t: raw.execute(f'SELECT * FROM {t}').fetchall() for t in TABLES}
    rollback(raw)
    for t in TABLES:
        assert raw.execute(f'SELECT * FROM {t}_retired_v10').fetchall() == before[t]
    with pytest.raises(SpawnerRollbackRefused, match='spawner_retired_tables_exist') as err:
        rollback(raw)
    assert all(t + '_retired_v10' in str(err.value) for t in TABLES)
    assert db_pg.migrate(raw, name) == [10]
    for t in TABLES:
        assert raw.execute(f'SELECT count(*) FROM {t}').fetchone() == (0,)
    with pytest.raises(SpawnerRollbackRefused, match='spawner_retired_tables_exist'):
        rollback(raw)
