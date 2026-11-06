"""C5 real Rust projection, disposable keeper only; no executing-host inputs."""
import json
from contextlib import contextmanager

import pytest

from plangraph.keeper import auth, db, placement_rules_bridge as bridge
from plangraph.keeper import placement_rules_report as projection
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store
from test_keeper_placement_bridge import rules_binary
from test_keeper_placement_rules import edit, KEY

MEMORY = 'PG_RULE_HOST_MEMORY_FLOOR_MIB'


def test_projection_is_auth_door():
    import fnmatch
    import inspect
    from pathlib import Path
    from tools.ci_commit_gate import AUTH_DOOR_PATHS
    root = Path(__file__).resolve().parents[1]
    for entrypoint in (projection.bounded_snapshot, projection.build_report):
        path = Path(inspect.getsourcefile(entrypoint)).resolve().relative_to(root).as_posix()
        assert any(fnmatch.fnmatchcase(path, p) for p in AUTH_DOOR_PATHS)


@pytest.fixture
def keeper(tmp_path, monkeypatch, rules_binary):
    monkeypatch.setattr(bridge, '_bin_env', lambda: {
        'PG_PLACEMENT_RULES_BIN': str(rules_binary)})
    conn = db.open_db(tmp_path / 'keeper.db', create=True, backend='sqlite')
    auth.add_principal(conn, 'router', 'router', ['project'])
    auth.add_principal(conn, 'reader', 'worker', ['project'])
    yield Store(conn), auth.Principal('router', 'router', ['project'])
    conn.close()


def test_fleet_and_class_seeds_exact(keeper):
    store, principal = keeper
    report = store.placement_rules_report(principal)
    assert report['fleet']['values'] == {MEMORY: 6144, KEY: 6}
    for name, cap in [('dispatch_vm', 4), ('prox', 5)]:
        assert report['classes'][name]['values'] == {MEMORY: 6144, KEY: cap}
    memory = next(s for s in report['specs'] if s['name'] == 'HostMemoryFloor')
    assert memory['alarm'] == 3000
    assert report['hosts'] == {}


def test_per_host_then_fleet_precedence(keeper):
    store, principal = keeper
    store.edit_placement_rule(principal, edit(value='7'))
    store.edit_placement_rule(principal, edit(1, '8', {'Host': 'prox'}))
    store.edit_placement_rule(principal, edit(2, '7000', {'Host': 'other'}, key=MEMORY))
    report = store.placement_rules_report(principal)
    assert report['fleet']['values'][KEY] == 7
    assert report['classes']['prox']['values'][KEY] == 7
    assert report['hosts']['prox']['values'][KEY] == 8
    assert report['hosts']['prox']['sources'][KEY] == 'keeper_host'
    assert report['hosts']['other']['values'][KEY] == 7
    assert report['hosts']['other']['sources'][KEY] == 'keeper_fleet'


def test_projection_labels_keeper_only_not_host_effective(keeper):
    store, principal = keeper
    store.edit_placement_rule(principal, edit(value='7000', scope={'Host': 'prox'}, key=MEMORY))
    report = store.placement_rules_report(principal)
    assert (report['context'], report['source'], report['revision']) == ('keeper_only', 'keeper', 1)
    assert report['hosts']['prox']['values'][KEY] == 6
    assert report['classes']['prox']['values'][KEY] == 5
    with store._write():
        assert store._placement_rules_projection() == report


@pytest.mark.parametrize('scope,host,key,value', [
    ('fleet', '', KEY, 'bad'), ('host', 'fixture', KEY, 'bad'),
    ('host', 'fixture', 'UNKNOWN', '4'), ('host', '\x7f', KEY, '4'),
])
def test_invalid_stored_override_fails_closed(keeper, scope, host, key, value):
    store, principal = keeper
    store.conn.execute('INSERT INTO placement_rule_values(scope,host,key,value) VALUES(?,?,?,?)',
                       (scope, host, key, value))
    with pytest.raises(Refused, match='rules_invalid'):
        store.placement_rules_report(principal)


def test_prospective_host_and_size_limits_atomic(keeper, monkeypatch):
    store, principal = keeper
    for i in range(128):
        store.edit_placement_rule(principal, edit(i, '4', {'Host': f'fixture-{i}'}))
    before = store.placement_rules(principal)
    with pytest.raises(Refused, match='rules_invalid'):
        store.edit_placement_rule(principal, edit(128, '4', {'Host': 'overflow'}))
    assert store.placement_rules(principal) == before
    assert store.placement_rule_audit(principal, since=128)['events'] == []
    # Exercise the exact serialized byte boundary without generating enormous valid values.
    prospective = json.loads(json.dumps(before))
    prospective['revision'] += 1
    prospective['fleet'][KEY] = '4'
    size = len(json.dumps(prospective, separators=(',', ':')).encode())
    monkeypatch.setattr(projection, 'MAX_SNAPSHOT_BYTES', size - 1)
    with pytest.raises(Refused, match='rules_invalid'):
        store.edit_placement_rule(principal, edit(128))
    assert store.placement_rules(principal) == before
    monkeypatch.setattr(projection, 'MAX_SNAPSHOT_BYTES', size)
    assert store.edit_placement_rule(principal, edit(128))['revision'] == 129


def test_non_ascii_host_envelope_boundary():
    host = 'é' * 32
    assert len(host.encode()) == 64
    snapshot = dict(revision=0, fleet={}, hosts={host: {}}, padding='')
    request = dict(op='report', snapshot=snapshot, host_class='dispatch_vm',
                   host_id=host, process={}, env_file={})

    def request_size():
        return len(json.dumps(request, separators=(',', ':')).encode()) + 1

    snapshot['padding'] = 'x' * (1024 * 1024 - request_size())
    assert request_size() == 1024 * 1024
    assert projection.bounded_snapshot(snapshot) == snapshot
    snapshot['padding'] += 'x'
    assert len(json.dumps(snapshot, separators=(',', ':')).encode()) < 1024 * 1024
    assert request_size() == 1024 * 1024 + 1
    with pytest.raises(Refused, match='rules_invalid'):
        projection.bounded_snapshot(snapshot)


@pytest.mark.parametrize('host', ['', '   ', '\x00', '\x85', 'a' * 65, 'é' * 33])
def test_invalid_host_ids_atomic(keeper, host):
    store, principal = keeper
    with pytest.raises(Refused, match='rules_invalid'):
        store.edit_placement_rule(principal, edit(scope={'Host': host}))
    assert store.placement_rules(principal)['revision'] == 0


def test_specs_equal_rust_schema(keeper):
    store, principal = keeper
    specs = bridge.call_rules({'op': 'specs'})['specs']
    assert store.placement_rule_specs(principal) == specs
    assert store.placement_rules_report(principal)['specs'] == specs


def test_no_process_environment_disclosed(keeper, monkeypatch):
    store, principal = keeper
    secret = 'private' + '-fixture'
    binary = bridge._bin_env()['PG_PLACEMENT_RULES_BIN']
    monkeypatch.setattr(bridge, '_bin_env', lambda: {
        'PG_PLACEMENT_RULES_BIN': binary, KEY: '99', 'TOKEN': secret})
    original = projection.call_rules
    requests = []

    def call(request):
        requests.append(request)
        assert request['process'] == request['env_file'] == {}
        return original(request)

    monkeypatch.setattr(projection, 'call_rules', call)
    report = store.placement_rules_report(principal)
    assert len(requests) == 3
    assert report['fleet']['values'][KEY] == 6
    assert secret not in json.dumps(report)


def test_read_auth_and_snapshot_materialized_before_helper(keeper, monkeypatch):
    store, principal = keeper
    reader = auth.Principal('reader', 'worker', ['project'])
    assert store.placement_rule_specs(reader)
    assert store.placement_rules_report(reader)['revision'] == 0
    for denied in [auth.RunPrincipal('run:fixture', 'worker'),
                   auth.OversightPrincipal('reader', 'worker', ['project']),
                   auth.Principal('system', 'system')]:
        for method in [store.placement_rules_report, store.placement_rule_specs]:
            with pytest.raises(Refused):
                method(denied)
    original = store._write
    active = []

    @contextmanager
    def write():
        active.append(True)
        with original():
            yield
        active.pop()

    build = projection.build_report

    def project(snapshot):
        assert not active
        store.edit_placement_rule(principal, edit())
        assert snapshot['revision'] == 0
        return build(snapshot)

    monkeypatch.setattr(store, '_write', write)
    monkeypatch.setattr('plangraph.keeper.store_placement_rules.build_report', project)
    assert store.placement_rules_report(principal)['revision'] == 0
