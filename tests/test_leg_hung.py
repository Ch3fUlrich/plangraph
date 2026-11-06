"""Synthetic boundary tests: no host transports or credential readers."""
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.spawner.hung_capture import HungBoundary
from plangraph.spawner.stall_probe import probe_run
from plangraph.spawner.stall_watchdog import tick


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    def deny(*a, **kw):
        raise AssertionError('external access forbidden')
    monkeypatch.setattr(socket, 'socket', deny)
    monkeypatch.setattr(subprocess, 'run', deny)
    monkeypatch.setattr('pathlib.Path.home', deny)
    monkeypatch.setattr('os.kill', deny)


def setup(tmp_path, **changes):
    calls = []
    evidence = dict(tokens=0, output_bytes=0, gateway_socket=False,
                    queue_limit=False, tree=[dict(pid=42, parent=1, start=7)],
                    logs=['queue_limit', 'discard arbitrary text'])
    evidence.update(changes)
    reader = SimpleNamespace(enabled=True)
    boundary = HungBoundary(reader, sample=lambda r: evidence,
                            identity=lambda p, i: i == 7,
                            cancel=lambda r: calls.append('cancel') or True,
                            fallthrough=lambda r: calls.append('fallthrough'), minutes=2)
    record = dict(run_id='run-synthetic', leg='provider/model', status='running',
                  pid=42, root_identity=7, started=0)
    return boundary, record, calls


def test_threshold_capture_before_cancel(tmp_path):
    b, r, calls = setup(tmp_path)
    assert probe_run(tmp_path, r, 119, hung=b)['rule'] == 'unknown'
    cancel = b.cancel
    def checked(record):
        path = tmp_path / 'runs/run-synthetic/leg-hung.json'
        data = json.loads(path.read_text())
        assert data['tree'] == [dict(pid=42, parent=1, start=7)]
        assert data['logs'] == ['queue_limit']
        assert path.stat().st_mode & 0o777 == 0o600
        calls.append('capture')
        return cancel(record)
    b.cancel = checked
    result = tick([r], [], lambda s: None, state=tmp_path, now=120, hung=b)
    assert result[0]['rule'] == 'leg_hung'
    assert calls == ['capture', 'cancel', 'fallthrough']


def test_queue_requires_positive_evidence_and_enabled_ranking(tmp_path):
    b, r, calls = setup(tmp_path, gateway_socket=True, queue_limit=True)
    assert probe_run(tmp_path, r, 120, hung=b)['rule'] == 'queue_wait'
    legs = ['provider/model', 'provider/other']
    assert b.rank(legs) == legs[::-1]
    b.reader.enabled = False
    assert b.rank(legs) == legs
    assert not calls
    b, r, calls = setup(tmp_path, gateway_socket=True)
    assert probe_run(tmp_path, r, 120, hung=b)['rule'] == 'unknown'
    assert not calls


@pytest.mark.parametrize('tokens', [None, False, '0', 1])
def test_unknown_or_nonzero_usage_never_cancels(tmp_path, tokens):
    b, r, calls = setup(tmp_path, tokens=tokens)
    assert probe_run(tmp_path, r, 120, hung=b)['rule'] == 'unknown'
    assert not calls


@pytest.mark.parametrize('mode', ['reuse', 'capture', 'dry', 'changed'])
def test_fail_closed(tmp_path, mode):
    b, r, calls = setup(tmp_path)
    if mode == 'reuse':
        b.identity = lambda *a: False
    if mode == 'capture':
        def fail(*a):
            raise OSError('synthetic')
        b.write = fail
    if mode == 'changed':
        checks = iter([True, True, False])
        b.identity = lambda *a: next(checks)
    result = probe_run(tmp_path, r, 120, hung=b, dry_run=mode == 'dry')
    assert not calls
    if mode == 'capture':
        assert result['capture_error'] == 'OSError'


def test_disabled_is_legacy(tmp_path, monkeypatch):
    b, r, calls = setup(tmp_path)
    b.reader.enabled = False
    r['status'] = 'queued'
    assert probe_run(tmp_path, r, 120, hung=b) == probe_run(tmp_path, r, 120)
    assert not calls


def test_existing_capture_is_preserved(tmp_path):
    b, r, calls = setup(tmp_path)
    path = tmp_path / 'runs/run-synthetic/leg-hung.json'
    path.parent.mkdir(parents=True)
    path.write_text('previous evidence')
    assert probe_run(tmp_path, r, 120, hung=b)['capture_error'] == 'FileExistsError'
    assert path.read_text() == 'previous evidence'
    assert not calls


@pytest.mark.parametrize('minutes', [0, -1, float('inf'), float('nan')])
def test_finite_threshold(tmp_path, minutes):
    b, _, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        HungBoundary(b.reader, sample=None, identity=None, cancel=None,
                     fallthrough=None, minutes=minutes)
