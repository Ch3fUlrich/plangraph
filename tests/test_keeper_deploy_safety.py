"""Recovery and running-process evidence for keeper deployment; no live services."""
import ast
import signal
import sys
from pathlib import Path

import pytest
import test_keeper_deploy as base

from plangraph.keeper.cli_verbs import Unreachable

kd, setup = base.kd, base.setup


def test_restore_complete_reload_failure_still_attempts_restart(setup):
    d, calls, ctl = setup
    def broken_reload(*args):
        if args == ('daemon-reload',) and calls.count(args) == 1:
            calls.append(args)
            raise OSError('rollback reload denied')
        return ctl(*args)
    code, reason = base.run(d, broken_reload, base.FakeSession(missing_route=True))
    assert code == 1 and 'rollback reload denied' in reason
    assert calls.count(('restart', kd.UNIT)) == 2
    assert 'rollback daemon-reload failed' in reason and 'rollback restart ok' in reason


def test_pinned_old_execstart_rolls_back_before_smoke(setup):
    d, _, ctl = setup
    (d / 'zzzzz-operator.conf').write_text(
        '[Service]\nExecStart=/usr/bin/python3 /old/src/plangraph/keeper/daemon.py\n')
    saved = kd.snapshot_dropins(d)
    session = base.FakeSession()
    code, reason = base.run(d, ctl, session)
    assert code == 1 and 'running ExecStart' in reason
    assert kd.snapshot_dropins(d) == saved
    assert session.calls == ['health']


def test_sha_mismatch_refuses_before_backup_or_write(setup):
    d, calls, ctl = setup
    saved = kd.snapshot_dropins(d)
    code, reason = kd.deploy('t', 'a' * 40, '/tree/live-bbbb', 'PROBE', base.FakeSession(),
                             dropin_dir=d, ctl=ctl)
    assert code == 1 and 'SHA' in reason
    assert kd.snapshot_dropins(d) == saved and not calls
    assert not list(d.parent.glob('unit.d.backup-*'))


def test_signal_is_ignored_before_recovery_condition(setup):
    d, _, ctl = setup
    tree = ast.parse(Path(kd.__file__).read_text())
    deploy = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_deploy')
    attempt = next(n for n in deploy.body if isinstance(n, ast.Try))
    guard = next(n for n in attempt.finalbody if isinstance(n, ast.If)).lineno
    injected = []
    def trace(frame, event, arg):
        if frame.f_code.co_filename == kd.__file__ and event == 'line' and frame.f_lineno == guard and not injected:
            injected.append(True)
            signal.raise_signal(signal.SIGTERM)
        return trace
    old_trace = sys.gettrace()
    result = None
    try:
        sys.settrace(trace)
        try:
            result = base.run(d, ctl, base.FakeSession(missing_route=True))
        except InterruptedError:
            pass
    finally:
        sys.settrace(old_trace)
    assert injected and result and result[0] == 1
    assert (d / 'live-sync-old-aaaa.conf').read_text() == 'OLD\n'


def test_nested_deadline_preserves_outer_timer():
    with kd.smoke_deadline(5):
        with kd.smoke_deadline(1):
            pass
        assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 5


def test_nested_deadline_cannot_extend_outer_timer():
    with kd.smoke_deadline(5), kd.smoke_deadline(50):
        assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 5


def test_expired_smoke_budget_does_not_start_release_retry():
    clock = [0]
    class Expired(base.FakeSession):
        def call(self, method, verb, params=None, body=None):
            if verb == 'release':
                self.calls.append(verb)
                clock[0] = 2
                raise Unreachable('release failed')
            return super().call(method, verb, params, body)
    session = Expired()
    assert 'failed' in kd.smoke(session, 'PROBE', 1, clock=lambda: clock[0])
    assert session.calls.count('release') == 1


def test_partial_restore_does_not_reload_or_restart(setup, monkeypatch):
    d, calls, ctl = setup
    (d / 'live-sync-second.conf').write_text('SECOND\n')
    write_bytes = Path.write_bytes
    def partial_write(path, data):
        if path == d / 'live-sync-second.conf':
            raise OSError('disk full halfway through restore')
        return write_bytes(path, data)
    monkeypatch.setattr(Path, 'write_bytes', partial_write)
    code, reason = base.run(d, ctl, base.FakeSession(missing_route=True))
    assert code == 1 and 'ROLLBACK FAILED' in reason and 'backup' in reason.lower()
    assert calls.count(('daemon-reload',)) == 1 and calls.count(('restart', kd.UNIT)) == 1
    backup = next(d.parent.glob('unit.d.backup-*'))
    assert (backup / 'live-sync-second.conf').read_text() == 'SECOND\n'
    assert (d / 'live-sync-old-aaaa.conf').read_text() == 'OLD\n'
    assert not (d / 'live-sync-second.conf').exists()


@pytest.mark.parametrize('relative', [False, True])
def test_tree_normalized_before_writing_or_restarting(setup, monkeypatch, relative):
    d, calls, ctl = setup
    tree = d.parent / 'tree'
    tree.mkdir()
    monkeypatch.chdir(d.parent)
    supplied = './tree' if relative else str(tree) + '/'
    code, reason = kd.deploy('t', 'b' * 40, supplied, 'PROBE', base.FakeSession(), dropin_dir=d, ctl=ctl)
    assert (code, reason) == (0, 'ok')
    text = (d / kd.MANAGED_DROPIN).read_text()
    assert f'WorkingDirectory={tree}\n' in text and f'Environment=PYTHONPATH={tree}/src\n' in text
    assert calls.count(('restart', kd.UNIT)) == 1


@pytest.mark.parametrize('mismatch', ['pid-zero', 'cwd', 'environment', 'pid-changed', 'missing-proc'])
def test_running_process_mismatch_refuses_and_restores(setup, mismatch):
    from fake_pid import FAKE_PID
    d, _, ctl = setup
    saved, queries = kd.snapshot_dropins(d), []
    def stale(*args):
        result = ctl(*args)
        if args == kd.RUNNING_SHOW:
            queries.append(True)
            if mismatch == 'pid-zero':
                return 'MainPID=0'
            if mismatch == 'pid-changed' and len(queries) == 2:
                return f'MainPID={FAKE_PID + 1}'
            proc = kd.PROC_ROOT / str(FAKE_PID)
            if mismatch == 'missing-proc':
                (proc / 'cmdline').unlink()
            if mismatch == 'cwd':
                (proc / 'cwd').unlink()
                (proc / 'cwd').symlink_to('/old/tree')
            if mismatch == 'environment':
                (proc / 'environ').write_bytes(b'PYTHONPATH=/old/tree/src\0TOKEN=private\0')
        return result
    session = base.FakeSession()
    code, reason = base.run(d, stale, session)
    assert code == 1 and 'running' in reason and 'private' not in reason
    assert session.calls == ['health'] and kd.snapshot_dropins(d) == saved


def test_matching_abbreviated_sha_is_checked(setup):
    d, _, ctl = setup
    assert kd.deploy('t', 'B' * 8, '/tree/live-bbbb', 'PROBE', base.FakeSession(), dropin_dir=d, ctl=ctl) == (0, 'ok')


def test_rollback_reports_reload_and_restart_failures(setup):
    d, calls, ctl = setup
    def broken(*args):
        if args[0] in ('daemon-reload', 'restart') and calls.count(args) == 1:
            calls.append(args)
            raise OSError('rollback ' + args[0])
        return ctl(*args)
    code, reason = base.run(d, broken, base.FakeSession(missing_route=True))
    assert code == 1 and 'rollback daemon-reload failed' in reason and 'rollback restart failed' in reason
    assert calls.count(('daemon-reload',)) == 2 and calls.count(('restart', kd.UNIT)) == 2


def test_systemctl_calls_keep_individual_timeout(monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(kd.subprocess, 'run', lambda command, **options:
                        calls.append((command, options)) or SimpleNamespace(returncode=0, stdout=''))
    kd.systemctl('daemon-reload')
    kd.systemctl('restart', kd.UNIT)
    assert len(calls) == 2 and all(options['timeout'] == 60 for _, options in calls)


def test_relative_tree_with_unsafe_resolved_parent_refuses_before_write(setup, monkeypatch):
    d, calls, ctl = setup
    parent = d.parent / 'unsafe parent'
    parent.mkdir()
    monkeypatch.chdir(parent)
    with pytest.raises(ValueError, match='resolved tree'):
        kd.deploy('t', 'b' * 40, './tree', 'PROBE', base.FakeSession(), dropin_dir=d, ctl=ctl)
    assert not calls and not list(d.parent.glob('unit.d.backup-*'))
