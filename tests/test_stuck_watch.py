"""Fake proc only: no processes are started or signalled."""
import json
import os

import pytest

from plangraph.spawner import stuck_watch as watch


@pytest.fixture(autouse=True)
def no_signals(monkeypatch):
    monkeypatch.setattr(os, 'kill', lambda *a: pytest.fail('live signal'))


@pytest.fixture
def seat(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    record = dict(run_id='run-test', status='running', pid=42, pid_start=7, started=0)
    calls = []
    def process(pid=42, parent=1, start=7, cpu=0, tty=0,
                cmd='plangraph-job-run-test'):
        directory = proc / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ['0'] * 22
        fields[0:5] = ['S', str(parent), '42', '42', str(tty)]
        fields[11], fields[19] = str(cpu), str(start)
        (directory / 'stat').write_text(f'{pid} (fake) ' + ' '.join(fields))
        (directory / 'cmdline').write_bytes(cmd.encode())
        (directory / 'fd').mkdir(exist_ok=True)
        (directory / 'net').mkdir(exist_ok=True)
        for name in ('tcp', 'tcp6'):
            (directory / 'net' / name).write_text('header\n')
    process()
    def check(now, **kw):
        return watch.check(tmp_path, [record] if record else [], now, proc=proc,
                           cancel=lambda *a: calls.append('cancel') or 'cancelled',
                           kill=lambda *a: calls.append('kill'), **kw)
    return tmp_path, proc, record, calls, process, check


def test_idle_tree_cancelled_at_300_not_298(seat):
    _, _, _, calls, _, check = seat
    assert not check(0)
    assert not check(298)
    assert check(300)[0]['rule'] == 'stuck-watch-idle'
    assert calls == ['cancel']


@pytest.mark.parametrize('change', ['cpu', 'start'])
def test_cpu_change_and_pid_reuse_reset(seat, change):
    _, _, _, calls, process, check = seat
    check(0)
    process(**{change: 8})
    check(300)
    assert not calls


def socket(seat, remote):
    _, proc, _, _, _, _ = seat
    (proc / '42/fd/3').symlink_to('socket:[99]')
    (proc / '42/net/tcp').write_text(
        'header\n0: 00000000:1234 ' + remote + ':443 01 0 0 0 0 0 99\n')


def test_buffered_seat_outbound_socket_no_cancel(seat):
    _, _, record, calls, _, check = seat
    check(0)
    socket(seat, '08080808')
    assert not check(526)
    record['status'] = 'completed'
    assert not calls


def test_outbound_socket_resets(seat):
    _, proc, _, calls, _, check = seat
    check(0)
    socket(seat, '08080808')
    check(298)
    (proc / '42/net/tcp').write_text('header\n')
    check(300)
    assert not calls


def test_loopback_only_does_not_reset(seat):
    *_, check = seat
    check(0)
    socket(seat, '0100007F')
    assert check(300)[0]['rule'] == 'stuck-watch-idle'


@pytest.mark.parametrize('shape', ['tty', 'service', 'unproven'])
def test_tty_service_unproven_report_only(seat, shape):
    _, _, record, calls, process, check = seat
    if shape == 'tty':
        process(tty=1)
    elif shape == 'service':
        process(pid=43, parent=42, cmd='opencode serve --service')
    else:
        record['pid_start'] = 99
    assert check(0)[0]['rule'] == 'stuck-watch-report'
    assert not calls


@pytest.mark.parametrize('missing', [False, True])
def test_terminal_missing_record_kill_proven(seat, missing):
    _, _, record, calls, _, check = seat
    check(0)
    if missing:
        record.clear()
    else:
        record['status'] = 'completed'
    assert check(120)[0]['rule'] == 'stuck-watch-reap'
    assert calls == ['kill']


@pytest.mark.parametrize('proven', [False, True])
def test_orphan_serve_both_ways(seat, proven, monkeypatch):
    _, _, _, calls, process, check = seat
    if proven:
        process(pid=43, cmd='opencode serve --stdio')
    else:
        process(cmd='opencode serve --stdio')
    result = check(0)
    assert result[0]['rule'] == ('stuck-watch-reap' if proven else 'stuck-watch-report')
    assert calls == (['kill'] if proven else [])


@pytest.mark.parametrize('failure', ['proc', 'socket', 'record'])
def test_unreadable_evidence_no_action(seat, failure):
    root, proc, record, calls, _, check = seat
    check(0)
    if failure == 'proc':
        (proc / '42/stat').write_text('broken')
    elif failure == 'socket':
        (proc / '42/net/tcp').unlink()
    else:
        record.clear()
        path = root / 'runs/run-test/record.json'
        path.parent.mkdir(parents=True)
        path.write_text('broken')
    assert not check(300)
    assert not calls


def test_dry_run_no_action_or_writes(seat):
    root, _, _, calls, _, check = seat
    check(0)
    path = root / 'watchdog/stuck-samples.json'
    before = path.read_bytes()
    assert check(300, dry_run=True)[0]['rule'] == 'stuck-watch-idle'
    assert path.read_bytes() == before
    assert not calls


def test_check_failure_leaves_tick_working(tmp_path, monkeypatch):
    from plangraph.spawner import stall_watchdog
    def fail(*a, **kw):
        raise RuntimeError('synthetic')
    monkeypatch.setattr(watch, 'check', fail)
    events = []
    result = stall_watchdog.tick([], [dict(key='plangraph:x', state='claimed', session_id='s')],
                                 lambda s: 0, now=2000, state=tmp_path, deliver=events.append, stuck=True)
    assert [r['rule'] for r in result] == ['stuck-watch-error', 'idle-claim']
    assert events
