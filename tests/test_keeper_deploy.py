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
    def ctl(*args):
        ctl_calls.append(args)
        if args[0] == 'show':
            effective = {}
            for path in sorted(d.glob('*.conf')):
                for line in path.read_text().splitlines():
                    key, _, value = line.partition('=')
                    if key in ('WorkingDirectory', 'Environment'):
                        effective[key] = value
            return '\n'.join(f'{key}={value}' for key, value in effective.items())
    return d, ctl_calls, ctl


def run(d, ctl, session):
    return kd.deploy("t1", "bbbb", "/tree/live-bbbb", "PROBE", session, dropin_dir=d, ctl=ctl,
                     timeout=1, sleep=lambda s: None)


def test_smoke_ok_keeps_new_dropin(setup):
    d, calls, ctl = setup
    session = FakeSession()
    assert run(d, ctl, session) == (0, "ok")
    assert (d / "zzzz-plangraph-live-sync.conf").exists()
    assert "PYTHONPATH=/tree/live-bbbb/src" in (d / "zzzz-plangraph-live-sync.conf").read_text()
    assert session.calls == ["health", "claim", "release", "close_merged"]
    assert calls == [("daemon-reload",), ("restart", kd.UNIT), kd.SHOW]


def test_smoke_fail_restores_previous_and_restarts(setup):
    d, calls, ctl = setup
    err = RefusedCli("bad_state", "cannot read decisions file: x")
    code, msg = run(d, ctl, FakeSession(claim_error=err))
    assert code == 1 and "cannot read decisions file" in msg and "restored" in msg
    assert sorted(p.name for p in d.iterdir()) == ["live-sync-old-aaaa.conf"]
    assert (d / "live-sync-old-aaaa.conf").read_text() == "OLD\n"
    assert calls == [("daemon-reload",), ("restart", kd.UNIT), kd.SHOW, ("daemon-reload",), ("restart", kd.UNIT)]


def test_missing_close_merged_route_fails(setup):
    d, _, ctl = setup
    code, msg = run(d, ctl, FakeSession(missing_route=True))
    assert code == 1 and "close_merged route missing" in msg
    assert not (d / "zzzz-plangraph-live-sync.conf").exists()


@pytest.mark.parametrize('error', [RuntimeError('restart failed'), OSError('restart failed')])
def test_restart_error_rolls_back(setup, error):
    d, calls, _ = setup

    def ctl(*a):
        calls.append(a)
        if a[0] == "restart" and calls.count(a) == 1:
            raise error

    code, msg = run(d, ctl, FakeSession())
    assert code == 1 and "restart failed" in msg
    assert not (d / "zzzz-plangraph-live-sync.conf").exists()


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
    assert not (d / 'zzzz-plangraph-live-sync.conf').exists()


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
    assert calls == [('daemon-reload',), ('restart', kd.UNIT), kd.SHOW, ('daemon-reload',), ('restart', kd.UNIT)]
    assert sorted(p.name for p in d.iterdir()) == ['live-sync-old-aaaa.conf']


def test_hung_claim_is_bounded_and_rolls_back(setup):
    d, _, ctl = setup
    class Slow(FakeSession):
        def call(self, method, verb, params=None, body=None):
            if verb == 'claim':
                time.sleep(.3)
            return super().call(method, verb, params, body)
    code, msg = kd.deploy('t', 'sha', '/tree', 'PROBE', Slow(), dropin_dir=d, ctl=ctl, timeout=.05)
    assert code == 1 and 'deadline' in msg
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


def test_claimant_token_smokes_real_role_checks(tmp_path, monkeypatch):
    from door_util import Doors
    from keeper_util import World, seed
    world = World(tmp_path)
    doors = None
    try:
        seed(world, [('PROBE', [])])
        doors = Doors(world, tmp_path)
        token = tmp_path / 'deploy.token'
        token.write_text(world.tok('l1main'))
        token.chmod(0o600)
        monkeypatch.setenv('PG_TOKEN_FILE', str(token))
        assert doors.http('router', 'POST', '/v1/claim', {'key': 'demo:PROBE'})[0] == 403
        assert doors.http('l1main', 'POST', '/v1/close_merged', {'dry_run': True})[0] == 403
        assert kd.smoke(kd.Session(url=doors.url), 'demo:PROBE', 5) is None
        assert world.store.show(world.p('router'), 'demo:PROBE')['state'] == 'open'
    finally:
        if doors:
            doors.close()
        world.close()


def test_later_managed_override_is_replaced(setup):
    d, calls, ctl = setup
    stale = d / 'live-sync-zzzz-ffff.conf'
    stale.write_text('[Service]\nWorkingDirectory=/stale\nEnvironment=PYTHONPATH=/stale/src\n')
    assert run(d, ctl, FakeSession()) == (0, 'ok')
    assert not stale.exists()
    assert sorted(p.name for p in d.glob('*.conf')) == ['zzzz-plangraph-live-sync.conf']
    assert any(call[0] == 'show' for call in calls)


def test_unmanaged_override_mismatch_rolls_back_before_smoke(setup):
    d, _, ctl = setup
    (d / 'zzzzz-operator.conf').write_text('[Service]\nWorkingDirectory=/stale\nEnvironment=PYTHONPATH=/stale/src\n')
    saved = kd.snapshot_dropins(d)
    session = FakeSession()
    code, reason = run(d, ctl, session)
    assert code == 1 and 'effective' in reason
    assert session.calls == ['health']  # rollback health only; never smoke the wrong tree
    assert kd.snapshot_dropins(d) == saved


@pytest.mark.parametrize('effective', [
    'WorkingDirectory=/tree/live-bbbb\nEnvironment=PYTHONPATH=/stale/src',
    'Environment=PYTHONPATH=/tree/live-bbbb/src',
])
def test_effective_unit_requires_both_tree_and_import_path(setup, effective):
    d, _, ctl = setup
    def wrong(*args):
        return effective if args[0] == 'show' else ctl(*args)
    session = FakeSession()
    assert run(d, wrong, session)[0] == 1
    assert session.calls == ['health']


def test_cli_defaults_to_preview(monkeypatch, capsys):
    monkeypatch.setattr(kd, 'deploy', lambda *a, **kw: pytest.fail('preview deployed'))
    assert kd.main(['--tag', 't', '--sha', 's', '--tree', '/tree', '--probe-key', 'PROBE']) == 0
    assert 'WorkingDirectory=/tree' in capsys.readouterr().out


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt(), SystemExit(2), 'SIGTERM', 'SIGINT'])
def test_interruption_after_write_restores_disk_backup(setup, interrupt, capsys):
    import signal
    d, _, ctl = setup
    saved = kd.snapshot_dropins(d)
    def interrupted(*args):
        if args[0] == 'show':
            backups = list(d.parent.glob('unit.d.backup-*'))
            assert len(backups) == 1
            assert kd.snapshot_dropins(backups[0]) == saved
            if isinstance(interrupt, str):
                signal.raise_signal(getattr(signal, interrupt))
            raise interrupt
        return ctl(*args)
    assert run(d, interrupted, FakeSession())[0] == 1
    assert kd.snapshot_dropins(d) == saved
    assert 'backup' in capsys.readouterr().out.lower()


def test_backup_failure_never_touches_dropins(setup, monkeypatch):
    d, calls, ctl = setup
    saved = kd.snapshot_dropins(d)
    monkeypatch.setattr(kd, 'backup_dropins', lambda *_: (_ for _ in ()).throw(OSError('disk full')))
    assert run(d, ctl, FakeSession())[0] == 1
    assert kd.snapshot_dropins(d) == saved
    assert calls == []


def test_restore_error_reported_and_restart_attempted(setup, monkeypatch):
    d, calls, ctl = setup
    monkeypatch.setattr(kd, 'restore_dropins', lambda *_: (_ for _ in ()).throw(OSError('restore denied')))
    code, msg = run(d, ctl, FakeSession(missing_route=True))
    assert code == 1 and 'ROLLBACK FAILED' in msg
    assert calls[-2:] == [('daemon-reload',), ('restart', kd.UNIT)]


@pytest.mark.parametrize('field', ['tag', 'sha', 'tree'])
@pytest.mark.parametrize('bad', ['has space', 'line\nbreak', 'control\x00', 'percent%h'])
def test_unit_arguments_reject_unsafe_values(field, bad):
    args = dict(tag='t', sha='sha', tree='/tree')
    args[field] = bad
    with pytest.raises(ValueError):
        kd.dropin_text(**args)


def test_failed_release_is_retried():
    class Retry(FakeSession):
        def call(self, method, verb, params=None, body=None):
            if verb == 'release' and self.calls.count('release') == 0:
                self.calls.append(verb)
                raise RefusedCli('temporary', 'retry')
            return super().call(method, verb, params, body)
    session = Retry()
    assert 'refused' in kd.smoke(session, 'PROBE', 1)
    assert session.calls.count('release') == 2


def test_apply_is_explicit(monkeypatch):
    calls = []
    monkeypatch.setattr(kd, 'deploy', lambda *a, **kw: (calls.append(a) or (0, 'ok')))
    assert kd.main(['--tag', 't', '--sha', 's', '--tree', '/tree', '--probe-key', 'PROBE', '--apply']) == 0
    assert len(calls) == 1


def test_rollback_reads_disk_backup(setup):
    d, _, ctl = setup
    def fail_after_write(*args):
        if args[0] == 'show':
            backup = next(d.parent.glob('unit.d.backup-*'))
            (backup / 'live-sync-old-aaaa.conf').write_text('DISK COPY\n')
            raise RuntimeError('verify failed')
        return ctl(*args)
    assert run(d, fail_after_write, FakeSession())[0] == 1
    assert (d / 'live-sync-old-aaaa.conf').read_text() == 'DISK COPY\n'
