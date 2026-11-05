"""Restart and fresh-state delivery regressions."""
import json
import subprocess
from pathlib import Path

from plangraph.spawner import remote_probe, stall_watchdog


def test_flush_fresh_state_creates_health_parent(tmp_path, monkeypatch):
    from plangraph.spawner import private_io
    write = private_io.atomic_write_private

    def require_parent(path, text):
        # Exercise the writer contract without implicit directory creation.
        if not Path(path).parent.is_dir():
            raise FileNotFoundError('missing parent')
        write(path, text)

    monkeypatch.setattr(private_io, 'atomic_write_private', require_parent)
    state = tmp_path / 'fresh'
    assert stall_watchdog.flush_outbox(state, ['notify']) == dict(
        failures=0, last_error=None)
    assert json.loads((state / 'watchdog/delivery.json').read_text()) == dict(
        failures=0, last_error=None)


def test_restart_flush_preserves_unmeasured_seen(tmp_path, monkeypatch):
    keys = [f'{i:064x}' for i in range(10000)]
    pending_key = 'down-fixture'
    path = tmp_path / 'remote-probe.json'
    path.write_text(json.dumps({'fixture': dict(
        failures=0, outage=0, down=False, seen=keys, acks=[],
        pending={pending_key: dict(rule='host-unreachable')})}))
    sent = []
    monkeypatch.setenv('PLANGRAPH_PROBE_ROOT', 'fixture')

    def run(argv, **kwargs):
        if argv[0] == 'ssh':
            raise subprocess.CalledProcessError(1, argv)
        sent.append(json.loads(kwargs['input'])['event_id'])

    monkeypatch.setattr(subprocess, 'run', run)
    remote_probe.poll(['fixture'], 'notify', tmp_path)
    entry = json.loads(path.read_text())['fixture']
    assert entry['seen'] == keys
    assert pending_key in entry['pending']
    assert sent == []

    def recovered(argv, **kwargs):
        if argv[0] == 'ssh':
            return subprocess.CompletedProcess(argv, 0, '{}')
        sent.append(json.loads(kwargs['input'])['event_id'])

    monkeypatch.setattr(subprocess, 'run', recovered)
    remote_probe.poll(['fixture'], 'notify', tmp_path)
    assert sent == [pending_key]
    assert json.loads(path.read_text())['fixture']['pending'] == {}
