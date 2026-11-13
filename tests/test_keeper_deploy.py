"""keeper_deploy: smoke ok keeps the new drop-in; smoke fail restores the old set (fakes only)."""
import importlib.util
from pathlib import Path
import time

import pytest

from plangraph.keeper.cli_verbs import RefusedCli

_spec = importlib.util.spec_from_file_location(
    "keeper_deploy", Path(__file__).resolve().parent.parent / "tools" / "keeper_deploy.py")
kd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kd)


class FakeSession:
    def __init__(self, claim_error=None, missing_route=False, route_error=None):
        self.calls, self.claim_error, self.missing_route = [], claim_error, missing_route
        self.route_error = route_error

    def call(self, method, verb, params=None, body=None):
        self.calls.append(verb)
        if verb == "claim" and self.claim_error:
            raise self.claim_error
        if verb == "close_merged" and self.missing_route:
            raise RefusedCli("not_found", "unknown path")
        if verb == "close_merged":
            if self.route_error:
                raise self.route_error
            assert body == {'dry_run': True}
            return {'dry_run': True, 'route': 'close_merged'}
        return {"epoch": 3}


@pytest.fixture
def setup(tmp_path):
    d = tmp_path / "unit.d"
    d.mkdir()
    (d / "live-sync-old-aaaa.conf").write_text("OLD\n")
    ctl_calls = []
    return d, ctl_calls, lambda *a: ctl_calls.append(a)


def run(d, ctl, session):
    return kd.deploy("t1", "bbbb", "/tree/live-bbbb", "PROBE", session, dropin_dir=d, ctl=ctl,
                     timeout=1, sleep=lambda s: None)


def test_smoke_ok_keeps_new_dropin(setup):
    d, calls, ctl = setup
    session = FakeSession()
    assert run(d, ctl, session) == (0, "ok")
    assert (d / "live-sync-t1-bbbb.conf").exists()
    assert "PYTHONPATH=/tree/live-bbbb/src" in (d / "live-sync-t1-bbbb.conf").read_text()
    assert session.calls == ["health", "claim", "release", "close_merged"]
    assert calls == [("daemon-reload",), ("restart", kd.UNIT)]


def test_smoke_fail_restores_previous_and_restarts(setup):
    d, calls, ctl = setup
    err = RefusedCli("bad_state", "cannot read decisions file: x")
    code, msg = run(d, ctl, FakeSession(claim_error=err))
    assert code == 1 and "cannot read decisions file" in msg and "restored" in msg
    assert sorted(p.name for p in d.iterdir()) == ["live-sync-old-aaaa.conf"]
    assert (d / "live-sync-old-aaaa.conf").read_text() == "OLD\n"
    assert calls == [("daemon-reload",), ("restart", kd.UNIT)] * 2


def test_missing_close_merged_route_fails(setup):
    d, _, ctl = setup
    code, msg = run(d, ctl, FakeSession(missing_route=True))
    assert code == 1 and "close_merged route missing" in msg
    assert not (d / "live-sync-t1-bbbb.conf").exists()


@pytest.mark.parametrize('error', [RuntimeError('restart failed'), OSError('restart failed')])
def test_restart_error_rolls_back(setup, error):
    d, calls, _ = setup

    def ctl(*a):
        calls.append(a)
        if a[0] == "restart" and calls.count(a) == 1:
            raise error

    code, msg = run(d, ctl, FakeSession())
    assert code == 1 and "restart failed" in msg
    assert not (d / "live-sync-t1-bbbb.conf").exists()


def test_validation_refusal_is_not_a_successful_dry_run(setup):
    d, _, ctl = setup
    assert run(d, ctl, FakeSession(route_error=RefusedCli("validation", "key required")))[0] == 1


@pytest.mark.parametrize('answer', [None, {}, {'dry_run': False}, {'dry_run': True, 'route': 'wrong'}])
def test_missing_dry_run_answer_rolls_back(setup, answer):
    d, _, ctl = setup
    class Wrong(FakeSession):
        def call(self, method, verb, params=None, body=None):
            if verb == 'close_merged':
                return answer
            return super().call(method, verb, params, body)
    assert run(d, ctl, Wrong())[0] == 1
    assert not (d / 'live-sync-t1-bbbb.conf').exists()


@pytest.mark.parametrize('error', [AttributeError('bad response'), ValueError('bad JSON')])
def test_unexpected_smoke_exception_rolls_back(setup, error):
    d, calls, ctl = setup
    class Broken(FakeSession):
        def call(self, method, verb, params=None, body=None):
            if verb == 'claim':
                raise error
            return super().call(method, verb, params, body)
    code, msg = run(d, ctl, Broken())
    assert code == 1 and str(error) in msg and 'restored' in msg
    assert calls == [('daemon-reload',), ('restart', kd.UNIT)] * 2
    assert sorted(p.name for p in d.iterdir()) == ['live-sync-old-aaaa.conf']


def test_hung_claim_is_bounded_and_rolls_back(setup):
    d, _, ctl = setup
    class Slow(FakeSession):
        def call(self, method, verb, params=None, body=None):
            if verb == 'claim':
                time.sleep(.3)
            return super().call(method, verb, params, body)
    start = time.monotonic()
    code, msg = kd.deploy('t', 'sha', '/tree', 'PROBE', Slow(), dropin_dir=d, ctl=ctl, timeout=.05)
    assert code == 1 and 'deadline' in msg and time.monotonic() - start < .2
    assert sorted(p.name for p in d.iterdir()) == ['live-sync-old-aaaa.conf']


def test_cli_dry_run_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(kd, 'deploy', lambda *a, **kw: pytest.fail('dry-run deployed'))
    assert kd.main(['--tag', 't', '--sha', 's', '--tree', '/tree', '--probe-key', 'PROBE', '--dry-run']) == 0
    assert 'WorkingDirectory=/tree' in capsys.readouterr().out


def test_close_merged_dry_run_route_is_authenticated_and_preserves_items(tmp_path):
    from door_util import Doors
    from keeper_util import World, seed
    world = World(tmp_path)
    doors = None
    try:
        seed(world, [('PROBE', [])])
        before = [tuple(r) for r in world.conn.execute('SELECT key,state,epoch FROM items ORDER BY key')]
        doors = Doors(world, tmp_path)
        assert doors.http('router', 'POST', '/v1/close_merged', {'dry_run': True}) == (
            200, {'dry_run': True, 'route': 'close_merged'})
        assert doors.http('workerA', 'POST', '/v1/close_merged', {'dry_run': True})[0] == 403
        assert doors.http('router', 'POST', '/v1/close_merged', {'dry_run': 'yes'})[0] == 400
        assert [tuple(r) for r in world.conn.execute('SELECT key,state,epoch FROM items ORDER BY key')] == before
    finally:
        if doors:
            doors.close()
        world.close()
