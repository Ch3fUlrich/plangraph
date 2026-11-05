import json
import sys
import subprocess
from pathlib import Path

import pytest

from plangraph.spawner.remote_probe import poll


def test_exchange_ack(tmp_path, monkeypatch):
    from plangraph.spawner.remote_probe import exchange
    from plangraph.spawner.stall_watchdog import deliver_event
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    event = dict(rule='run-orphan', run_id='fixture', kind='completion')
    deliver_event(tmp_path, event)
    findings = exchange([])
    assert list(findings.values()) == [event]
    assert exchange([]) == findings
    assert exchange(list(findings)) == {}
    deliver_event(tmp_path, event)
    assert exchange([]) == {}


def test_polls(tmp_path, monkeypatch):
    ssh = tmp_path / 'ssh'
    reply = tmp_path / 'reply'
    reply.write_text(json.dumps({'a' * 64: {'rule': 'run-silent'}}))
    ssh.write_text('#!' + sys.executable + '\nimport os, pathlib\n'
                   'print(pathlib.Path(os.environ["REPLY"]).read_text())\n')
    ssh.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setenv('REPLY', str(reply))
    sink = tmp_path / 'sink.py'
    output = tmp_path / 'notifications'
    sink.write_text('import sys\nwith open(sys.argv[1], "a") as f: f.write(sys.stdin.read())\n')
    command = f'{sys.executable} {sink} {output}'
    def once():
        poll(['fixture'], command, tmp_path)
    once()
    once()
    assert len(output.read_text().splitlines()) == 1
    reply.write_text('garbled')
    once()
    assert len(output.read_text().splitlines()) == 1
    once()
    once()
    assert len(output.read_text().splitlines()) == 2
    reply.write_text('{}')
    once()
    once()
    assert [json.loads(s)['rule'] for s in output.read_text().splitlines()] == [
        'run-silent', 'host-unreachable', 'host-recovered']


@pytest.mark.parametrize('raw', ['{', '[]', 'null', '{"fixture": {}}',
                                  '{"fixture": null}'])
def test_corrupt_state(tmp_path, monkeypatch, raw):
    (tmp_path / 'remote-probe.json').write_text(raw)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, '{}'))
    poll(['fixture'], 'notify', tmp_path)
    assert json.loads((tmp_path / 'remote-probe.json').read_text())['fixture']['acks'] == []


def test_outage_notify_retry(tmp_path, monkeypatch):
    calls = []
    def run(argv, **kwargs):
        if argv[0] == 'ssh' or not calls:
            calls.append(argv[0]) if argv[0] != 'ssh' else None
            raise subprocess.CalledProcessError(1, argv)
        calls.append(argv[0])
    monkeypatch.setattr(subprocess, 'run', run)
    for _ in range(2):
        poll(['fixture'], 'notify', tmp_path)
    assert not json.loads((tmp_path / 'remote-probe.json').read_text())['fixture']['down']
    poll(['fixture'], 'notify', tmp_path)
    poll(['fixture'], 'notify', tmp_path)
    assert calls == ['notify', 'notify']


def test_known_crash_window_documented():
    text = Path('docs/operations/spawner-remote-probes.md').read_text()
    assert 'at-least-once, not exactly-once' in text
    assert 'crash after the notify command' in text
