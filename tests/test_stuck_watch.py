"""Fake proc only: no processes are started or signalled."""
import json
import os

import pytest

from plangraph.spawner import stuck_watch as watch
from fake_pid import FAKE_PID


@pytest.fixture(autouse=True)
def no_signals(monkeypatch):
    monkeypatch.setattr(os, 'kill', lambda *a: pytest.fail('live signal'))


@pytest.fixture
def seat(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    record = dict(run_id='run-test', status='running', pid=FAKE_PID, pid_start=7, started=0)
    calls = []
    def process(pid=FAKE_PID, parent=1, start=7, cpu=0, tty=0,
                cmd='plangraph-job-run-test'):
        directory = proc / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ['0'] * 22
        fields[0:5] = ['S', str(parent), str(FAKE_PID), str(FAKE_PID), str(tty)]
        fields[11], fields[19] = str(cpu), str(start)
        (directory / 'stat').write_text(f'{pid} (fake) ' + ' '.join(fields))
        (directory / 'cmdline').write_bytes(cmd.encode())
        (directory / 'fd').mkdir(exist_ok=True)
        (directory / 'net').mkdir(exist_ok=True)
        for name in ('tcp', 'tcp6', 'udp', 'udp6'):
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


def test_watchdog_tick_records_process_evidence(seat, monkeypatch, tmp_path_factory):
    from plangraph.spawner import stall_watchdog
    from conftest_watchdog import isolated_process_evidence
    root, proc, record, calls, _, _ = seat
    real_check = watch.check
    monkeypatch.setattr(os, 'kill', lambda pid, signal: None if signal == 0 else pytest.fail('live signal'))
    monkeypatch.setattr(watch, 'check', lambda state, runs, now, **kw:
                        real_check(state, runs, now, proc=proc, **kw))
    with monkeypatch.context() as isolation:
        isolated_process_evidence.__wrapped__(isolation, tmp_path_factory)
        stall_watchdog.tick([record], [], lambda _: None, now=0, state=root, stuck=True)
        assert json.loads((root / 'watchdog/stuck-samples.json').read_text()) == {}
    stall_watchdog.tick([record], [], lambda _: None, now=0, state=root, stuck=True)
    samples = json.loads((root / 'watchdog/stuck-samples.json').read_text())
    assert samples['run-test']['identity'] == {str(FAKE_PID): 7}
    assert not calls


@pytest.mark.parametrize('change', ['cpu', 'start'])
def test_cpu_change_and_pid_reuse_reset(seat, change):
    _, _, _, calls, process, check = seat
    check(0)
    process(**{change: 8})
    check(300)
    assert not calls


def socket(seat, remote):
    _, proc, _, _, _, _ = seat
    (proc / f'{FAKE_PID}/fd/3').symlink_to('socket:[99]')
    (proc / f'{FAKE_PID}/net/tcp').write_text(
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
    (proc / f'{FAKE_PID}/net/tcp').write_text('header\n')
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
        process(pid=43, parent=FAKE_PID, cmd='opencode serve --service')
    else:
        record['pid_start'] = 99
    assert check(0)[0]['rule'] == 'stuck-watch-report'
    check(300)
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
        (proc / f'{FAKE_PID}/stat').write_text('broken')
    elif failure == 'socket':
        (proc / f'{FAKE_PID}/net/tcp').unlink()
    else:
        record.clear()
        path = root / 'runs/run-test/record.json'
        path.parent.mkdir(parents=True)
        path.write_text('broken')
    findings = check(300)
    if failure == 'socket':  # report-only, like a racing fd
        assert findings[0]['reason'] == 'process/socket evidence unavailable'
    else:
        assert not findings
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


def test_unrelated_proc_race_does_not_reset_idle(seat):
    _, proc, _, calls, _, check = seat
    check(0)
    (proc / '99').mkdir()
    assert check(300)[0]['rule'] == 'stuck-watch-idle'
    assert calls == ['cancel']


def test_unavailable_table_preserves_samples(seat, monkeypatch):
    root, _, _, _, _, check = seat
    check(0)
    path = root / 'watchdog/stuck-samples.json'
    before = path.read_bytes()
    def unavailable(*args):
        raise OSError('unavailable')
    monkeypatch.setattr(watch, 'processes', unavailable)
    assert check(120) == []
    assert path.read_bytes() == before


def test_absolute_job_title_matches_record(seat):
    _, _, _, calls, process, check = seat
    process(cmd='/usr/bin/plangraph-job-run-test')
    assert not check(0)
    assert check(300)[0]['run_id'] == 'run-test'
    assert calls == ['cancel']


@pytest.mark.parametrize('shape', ['idle', 'terminal', 'unclaimed', 'protected'])
def test_repeats_are_persistently_limited(seat, shape, monkeypatch):
    root, _, record, calls, process, check = seat
    monkeypatch.setenv('PLANGRAPH_WATCHDOG_REALERT_SECONDS', '1800')
    if shape == 'terminal':
        record['status'] = 'completed'
    elif shape == 'unclaimed':
        process(cmd='opencode serve --stdio')
    elif shape == 'protected':
        process(tty=1)
    if shape == 'idle':
        check(0)
    assert check(300)
    assert not check(420)
    sample = json.loads((root / 'watchdog/stuck-samples.json').read_text())
    assert next(iter(sample.values()))['last_alert'] == 300
    if shape in ('idle', 'terminal'):
        assert next(iter(sample.values()))['last_action'] == 300
    assert check(2100)
    assert len(calls) == (0 if shape in ('unclaimed', 'protected') else 2)


def test_empty_stuck_findings_keep_legacy_probe_diagnostic(tmp_path, monkeypatch):
    from plangraph.spawner import stall_watchdog
    monkeypatch.setattr(watch, 'check', lambda *a, **kw: [])
    seen = []
    monkeypatch.setattr(stall_watchdog, 'probe_run',
                        lambda *a, **kw: seen.append(kw['dry_run']))
    stall_watchdog.tick([dict(status='running')], [], lambda s: None,
                        state=tmp_path, stuck=True)
    assert seen == [True]


def test_platform_limitation_is_not_routed(tmp_path, monkeypatch):
    from plangraph.spawner import stall_watchdog
    original = watch.check
    monkeypatch.setattr(watch, 'check',
                        lambda *a, **kw: original(*a, **kw, platform='win32'))
    events = []
    for _ in range(2):
        assert stall_watchdog.tick([], [], lambda s: None, state=tmp_path,
                                   stuck=True, deliver=events.append)
    assert not events


def test_new_condition_is_not_suppressed(seat):
    _, _, record, calls, _, check = seat
    check(0)
    assert check(300)[0]['rule'] == 'stuck-watch-idle'
    record['status'] = 'completed'
    assert check(420)[0]['rule'] == 'stuck-watch-reap'
    assert calls == ['cancel', 'kill']


def test_reused_unclaimed_pid_is_not_suppressed(seat):
    _, _, _, calls, process, check = seat
    process(cmd='/usr/bin/opencode serve --stdio')
    assert check(0)
    assert not check(120)
    process(start=8, cmd='/usr/bin/opencode serve --stdio')
    assert check(240)
    assert not calls


@pytest.mark.parametrize('shape', ['missing', 'pid-mismatch', 'start-mismatch'])
def test_unproven_second_tick_never_actions(seat, shape):
    root, _, record, calls, _, check = seat
    if shape == 'missing':
        record.clear()
    elif shape == 'pid-mismatch':
        record['pid'] = 99
    else:
        record['pid_start'] = 99
    check(0)
    check(300)
    assert not calls
    sample = json.loads((root / 'watchdog/stuck-samples.json').read_text())['run-test']
    assert sample['proven'] is False


@pytest.mark.parametrize('protocol,state', [('udp', '01'), ('udp6', '01'),
                                          ('tcp', '02'), ('tcp6', '08')])
def test_connected_network_prevents_cancel(seat, protocol, state):
    _, proc, _, calls, _, check = seat
    check(0)
    remote = '08080808' if not protocol.endswith('6') else '00000000000000000000000008080808'
    (proc / f'{FAKE_PID}/fd/3').symlink_to('socket:[99]')
    (proc / f'{FAKE_PID}/net' / protocol).write_text(
        f'header\n0: 00000000:1234 {remote}:0043 {state} 0 0 0 0 0 99\n')
    assert not check(300)
    assert not calls


def test_ipv4_mapped_unspecified_remote_is_not_activity(seat):
    _, proc, _, calls, _, check = seat
    check(0)
    (proc / f'{FAKE_PID}/fd/3').symlink_to('socket:[99]')
    (proc / f'{FAKE_PID}/net/tcp6').write_text(
        'header\n0: 00000000:1234 0000000000000000FFFF000000000000:0043 08 0 0 0 0 0 99\n')
    assert check(300)
    assert calls


def test_two_hosts_do_not_share_proof_or_idle_interval(seat, monkeypatch):
    root, _, record, calls, _, check = seat
    monkeypatch.setenv('PLANGRAPH_HOST', 'host-a')
    check(0)
    monkeypatch.setenv('PLANGRAPH_HOST', 'host-b')
    assert not check(300)
    finding = check(600)[0]
    assert finding['host'] == 'host-b'
    from plangraph.spawner.stall_watchdog import status
    assert json.loads(status([finding]).removeprefix('! stall '))['host'] == 'host-b'
    sample = json.loads((root / 'watchdog/stuck-samples.json').read_text())['run-test']
    assert sample['host'] == 'host-b'
    record.clear()
    monkeypatch.setenv('PLANGRAPH_HOST', 'host-a')
    assert check(900)[0]['rule'] == 'stuck-watch-report'
    assert calls == ['cancel']


def test_racing_fd_reports_unavailable_and_resets_interval(seat, monkeypatch):
    from pathlib import Path
    _, proc, _, calls, _, check = seat
    check(0)
    fd = proc / f'{FAKE_PID}/fd/3'
    fd.symlink_to('socket:[99]')
    original = Path.readlink
    def racing(path):
        if path == fd:
            raise FileNotFoundError('racing fd')
        return original(path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'readlink', racing)
        finding = check(300)[0]
        assert finding['rule'] == 'stuck-watch-report'
        assert finding['reason'] == 'process/socket evidence unavailable'
    assert not check(301)
    assert not calls


@pytest.mark.parametrize('fault', [FileNotFoundError('gone'), ''])
def test_unreadable_net_table_reports_unavailable(seat, monkeypatch, fault):
    from pathlib import Path
    _, _, _, calls, _, check = seat
    check(0)
    original = Path.read_text
    def broken(path, *args, **kwargs):
        if path.name == 'tcp':
            if fault:
                raise fault
            return fault
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', broken)
        assert check(300)[0]['reason'] == 'process/socket evidence unavailable'
    assert not calls


def test_new_mismatched_record_overrides_previous_proof(seat):
    _, _, record, calls, _, check = seat
    check(0)
    record['pid'] = 99
    assert check(300)[0]['rule'] == 'stuck-watch-report'
    check(600)
    assert not calls
