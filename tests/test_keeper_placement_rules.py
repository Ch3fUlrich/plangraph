"""C4 same adapter contract on disposable SQLite and PostgreSQL schemas."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import uuid

import pytest

from plangraph.keeper import auth, db, db_pg, placement_rules_bridge
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store
from test_keeper_placement_bridge import rules_binary  # shared session fixture

KEY = 'PG_RULE_HOST_WORKER_CAP_COUNT'


def test_placement_modules_are_auth_doors():
    import fnmatch
    from tools.ci_commit_gate import AUTH_DOOR_PATHS
    for name in ('store_placement_rules.py', 'placement_rules_bridge.py'):
        path = 'src/plangraph/keeper/' + name
        assert any(fnmatch.fnmatchcase(path, pattern) for pattern in AUTH_DOOR_PATHS)


def test_pg_rollback_composes_migration_guard():
    from contextlib import nullcontext
    sql = pytest.importorskip('psycopg').sql
    from plangraph.keeper import placement_rules_schema as rules

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, params=()):
            if isinstance(query, sql.Composed):
                assert query.as_string() == (
                    'SELECT 1 FROM "fixture".schema_migrations WHERE version=11')
                raise RuntimeError('composed migration guard reached')
            self.query = query
            return self

        def fetchone(self):
            return (None,) if 'to_regclass' in self.query else ('fixture',)

        def fetchall(self):
            return []

    with pytest.raises(RuntimeError, match='composed migration guard reached'):
        rules.rollback_pg(Connection())


def test_mutation_and_audit_match_c1_serde_fields(backend):
    import re
    store, router, _ = backend
    source = (Path(__file__).resolve().parents[1] /
              'rust/worker-core/src/placement_rule_store.rs').read_text()
    body = source.split('pub struct RuleAudit {', 1)[1].split('}', 1)[0]
    fields = set(re.findall(r'pub (\w+):', body))
    result = store.edit_placement_rule(router, edit())
    event = store.placement_rule_audit(router)['events'][0]
    assert set(result) == set(event) == fields
    assert result == event


def edit(revision=0, value='4', scope='Fleet', **extra):
    return dict(key=KEY, value=value, scope=scope, expected_revision=revision,
                rollback_of=None) | extra


@pytest.fixture(params=['sqlite', 'postgres'])
def backend(request, tmp_path, monkeypatch, rules_binary):
    monkeypatch.setattr(placement_rules_bridge, '_bin_env',
                        lambda: {'PG_PLACEMENT_RULES_BIN': str(rules_binary)})
    connections = []
    if request.param == 'sqlite':
        def connect():
            path = tmp_path / 'keeper.db'
            conn = db.open_db(path, create=not path.exists(), backend='sqlite')
            connections.append(conn)
            return conn
        cleanup = lambda: None
    else:
        if not os.environ.get('PG_DSN'):
            pytest.skip('ephemeral PG fixture not enabled')
        import psycopg
        schema_name = 't_c4_' + uuid.uuid4().hex[:12]
        raw = psycopg.connect(os.environ['PG_DSN'], autocommit=True)
        db_pg.migrate(raw, schema_name)

        def connect():
            from psycopg import sql
            session = psycopg.connect(os.environ['PG_DSN'], autocommit=True)
            session.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema_name)))
            conn = db_pg.PgConnection(session, schema_name)
            connections.append(conn)
            return conn

        def cleanup():
            from psycopg import sql
            raw.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema_name)))
            raw.close()
    conn = connect()
    for name, role in [('router', 'router'), ('reader', 'worker'), ('manager', 'l1')]:
        auth.add_principal(conn, name, role, ['project'])
    yield Store(conn), auth.Principal('router', 'router', ['project']), connect
    for conn in connections:
        conn.close()
    cleanup()


def test_router_only_edit_and_audit(backend):
    store, router, _ = backend
    for name, role in [('reader', 'worker'), ('manager', 'l1')]:
        principal = auth.Principal(name, role, ['project'])
        assert store.placement_rules(principal)['revision'] == 0
        for call in [lambda: store.edit_placement_rule(principal, edit()),
                     lambda: store.placement_rule_audit(principal)]:
            with pytest.raises(Refused, match='forbidden'):
                call()
    assert store.edit_placement_rule(router, edit())['actor'] == 'router'


def test_run_oversight_l1_reader_and_revoked_refused(backend):
    store, router, _ = backend
    for principal in [auth.RunPrincipal('run:fixture', 'worker'),
                      auth.OversightPrincipal('reader', 'worker', ['project']),
                      auth.Principal('system', 'system')]:
        for call in [lambda: store.placement_rules(principal),
                     lambda: store.edit_placement_rule(principal, edit()),
                     lambda: store.placement_rule_audit(principal)]:
            with pytest.raises(Refused):
                call()
    auth.revoke(store.conn, router.name)
    with pytest.raises(Refused, match='forbidden'):
        store.edit_placement_rule(router, edit())


@pytest.mark.parametrize('change', [
    {'actor': 'router'}, {'role': 'router'}, {'expected_revision': True},
    {'expected_revision': -1}, {'expected_revision': 2**63}, {'rollback_of': True},
    {'key': 'UNKNOWN'}, {'value': 'invalid'}, {'scope': {'Host': ''}},
    {'rollback_of': -1}, {'rollback_of': 2**63}, {'scope': 'Invalid'},
])
def test_actor_body_cannot_spoof_and_invalid_no_mutation(backend, change):
    store, router, _ = backend
    with pytest.raises(Refused, match='rules_invalid'):
        store.edit_placement_rule(router, edit() | change)
    assert store.placement_rules(router) == dict(revision=0, fleet={}, hosts={})
    assert store.placement_rule_audit(router)['events'] == []


@pytest.mark.parametrize('kind', ['nan', 'inf', '-inf', 'object', 'cycle', 'deep'])
def test_unserializable_edit_refused_without_mutation(backend, kind):
    store, router, _ = backend
    if kind == 'object':
        value = object()
    elif kind == 'cycle':
        value = []
        value.append(value)
    elif kind == 'deep':
        value = '4'
        for _ in range(2000):
            value = [value]
    else:
        value = float(kind)
    with pytest.raises(Refused, match='rules_invalid'):
        store.edit_placement_rule(router, edit(value=value))
    assert store.placement_rules(router) == dict(revision=0, fleet={}, hosts={})
    assert store.placement_rule_audit(router)['events'] == []


def test_stale_duplicate_conflict_and_absent_delete_invalid(backend):
    store, router, _ = backend
    with pytest.raises(Refused, match='rules_invalid'):
        store.edit_placement_rule(router, edit(value=None))
    store.edit_placement_rule(router, edit())
    with pytest.raises(Refused, match='rules_conflict'):
        store.edit_placement_rule(router, edit())


def test_rollback_new_revision_previous_value_and_initial_insert_deletion(backend):
    store, router, _ = backend
    scope = {'Host': 'fixture'}
    first = store.edit_placement_rule(router, edit(scope=scope))
    assert first['previous'] is None
    store.edit_placement_rule(router, edit(1, '5', scope))
    rollback = store.edit_placement_rule(router, edit(2, '4', scope, rollback_of=2))
    assert rollback['previous'] == '5'
    store.edit_placement_rule(router, edit(3, None, scope, rollback_of=1))
    assert store.placement_rules(router) == dict(revision=4, fleet={}, hosts={})
    page = store.placement_rule_audit(router, limit=2)
    assert [e['revision'] for e in page['events']] == [1, 2]
    assert page['next_revision'] == 2
    assert len(store.placement_rule_audit(router, since=2)['events']) == 2


@pytest.mark.parametrize('change', [
    {'rollback_of': 99}, {'scope': {'Host': 'other'}}, {'value': '5'},
    {'key': 'PG_RULE_HOST_MEMORY_FLOOR_MIB'},
])
def test_cross_scope_key_missing_rollback_refused(backend, change):
    store, router, _ = backend
    store.edit_placement_rule(router, edit())
    revision = 1
    if 'key' in change:
        store.edit_placement_rule(router, edit(1, '4096', key=change['key']))
        revision = 2
    with pytest.raises(Refused, match='rules_invalid'):
        store.edit_placement_rule(router, edit(revision, None, rollback_of=1) | change)
    assert store.placement_rules(router)['revision'] == revision


def test_fault_after_value_before_audit_rolls_back(backend):
    store, router, _ = backend
    real = store.conn

    class Fault:
        def execute(self, sql, params=()):
            if sql.startswith('INSERT INTO placement_rule_audit'):
                raise RuntimeError('fixture fault')
            return real.execute(sql, params)

    store.conn = Fault()
    with pytest.raises(RuntimeError, match='fixture fault'):
        store.edit_placement_rule(router, edit())
    store.conn = real
    assert store.placement_rules(router) == dict(revision=0, fleet={}, hosts={})
    assert store.placement_rule_audit(router)['events'] == []


def test_two_connections_same_revision_one_winner(backend):
    store, router, connect = backend
    barrier = threading.Barrier(2)

    def attempt():
        other = Store(connect())
        barrier.wait(timeout=5)
        try:
            return other.edit_placement_rule(router, edit())['revision']
        except Refused as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(map(str, results)) == ['1', 'rules_conflict']
    assert len(store.placement_rule_audit(router)['events']) == 1


def test_coherent_snapshot(backend):
    store, router, connect = backend
    store.edit_placement_rule(router, edit())
    other = Store(connect())
    snapshot = other.placement_rules(router)
    assert snapshot == dict(revision=1, fleet={KEY: '4'}, hosts={})

    # A writer on a second connection cannot slip a new value between the
    # reader's revision query and value hydration under the schema write lock.
    entered = threading.Event()
    finished = threading.Event()
    real = other.conn

    class Pause:
        def execute(self, sql, params=()):
            result = real.execute(sql, params)
            if sql.startswith('SELECT revision FROM placement_rule_revision'):
                entered.set()
                assert not finished.wait(0.1)
            return result

    def update():
        writer = Store(connect())
        assert entered.wait(5)
        writer.edit_placement_rule(router, edit(1, '5'))
        finished.set()

    other.conn = Pause()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(update)
        assert other.placement_rules(router) == snapshot
        pending.result(timeout=10)
    other.conn = real
    assert other.placement_rules(router) == dict(revision=2, fleet={KEY: '5'}, hosts={})


def test_auth_rechecked_inside_transaction(backend, monkeypatch):
    store, router, _ = backend
    original = store._check_principal
    calls = []

    def check(principal):
        calls.append(principal)
        if len(calls) == 3:
            raise Refused('forbidden', 'fixture revoked at lock')
        original(principal)

    monkeypatch.setattr(store, '_check_principal', check)
    with pytest.raises(Refused, match='forbidden'):
        store.edit_placement_rule(router, edit())
    assert len(calls) == 3
    assert store._placement_rules_snapshot()['revision'] == 0


@pytest.mark.parametrize('missing', ['value', 'rollback_of', 'expected_revision', 'scope', 'key'])
def test_missing_fields_refused(backend, missing):
    store, router, _ = backend
    request = edit()
    del request[missing]
    with pytest.raises(Refused, match='rules_invalid'):
        store.edit_placement_rule(router, request)
