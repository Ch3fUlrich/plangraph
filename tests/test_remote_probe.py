import json
import sys
import subprocess
from pathlib import Path

import pytest

from plangraph.spawner.remote_probe import poll


@pytest.fixture(autouse=True)
def probe_root(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_PROBE_ROOT', 'fixture root')


def test_exchange_ack(tmp_path, monkeypatch):
    from plangraph.spawner.remote_probe import exchange
    from plangraph.spawner.stall_watchdog import deliver_event
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    event = dict(rule='run-orphan', run_id='fixture', kind='completion')
    deliver_event(tmp_path, event)
    findings = exchange([])
    assert list(findings.values())[0].items() >= event.items()
    assert list(findings.values())[0]['event_id']
    assert exchange([]) == findings
    assert exchange(list(findings)) == {}
    deliver_event(tmp_path, dict(event, age=240, seconds_silent=180))
    assert exchange([]) == {}


def test_polls(tmp_path, monkeypatch):
    ssh = tmp_path / 'ssh'
    reply = tmp_path / 'reply'
    reply.write_text(json.dumps({'a' * 64: {'rule': 'run-silent'}}))
    ssh.write_text('#!' + sys.executable + '\nimport os, pathlib\n'
                   'import sys\nassert sys.argv[-1].startswith("cd \'fixture root\' && PYTHONPATH=src python3 -c " )\n'
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
    events = [json.loads(s) for s in output.read_text().splitlines()]
    assert events[0]['event_id'] == 'a' * 64
    assert events[1]['event_id'].removeprefix('down-') == events[2]['event_id'].removeprefix('up-')
    (tmp_path / 'remote-probe.json').unlink()
    reply.write_text('garbled')
    once()
    once()
    assert json.loads(output.read_text().splitlines()[-1])['event_id'] != events[1]['event_id']


def test_missing_root_main(monkeypatch, capsys):
    from plangraph.spawner.remote_probe import main
    monkeypatch.delenv('PLANGRAPH_PROBE_ROOT')
    monkeypatch.setattr(sys, 'argv', ['remote_probe', '--host', 'fixture', '--notify-cmd', 'true'])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert 'PLANGRAPH_PROBE_ROOT is required' in capsys.readouterr().err


def test_missing_root_poll(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_PROBE_ROOT')
    poll(['fixture'], 'true', tmp_path)
    assert json.loads((tmp_path / 'remote-probe.json').read_text())['fixture']['failures'] == 1


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


@pytest.mark.parametrize('failure', [SystemExit, subprocess.CalledProcessError, subprocess.TimeoutExpired])
def test_failed_notify_retries(tmp_path, monkeypatch, failure):
    calls = []
    def run(argv, **kwargs):
        if argv[0] == 'ssh':
            return subprocess.CompletedProcess(argv, 0, json.dumps({'a' * 64: {'rule': 'run-silent'}}))
        calls.append(argv)
        if len(calls) == 1:
            raise failure() if failure is SystemExit else failure(1, argv)
    monkeypatch.setattr(subprocess, 'run', run)
    if failure is SystemExit:
        with pytest.raises(SystemExit):
            poll(['fixture'], 'notify', tmp_path)
    else:
        poll(['fixture'], 'notify', tmp_path)
    poll(['fixture'], 'notify', tmp_path)
    assert len(calls) == 2
    assert json.loads((tmp_path / 'remote-probe.json').read_text())['fixture']['acks'] == ['a' * 64]


def test_completion_dispositions_reach_notify(tmp_path, monkeypatch):
    from plangraph.spawner.remote_probe import exchange
    from plangraph.spawner.stall_watchdog import deliver_event
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    calls = []
    def run(argv, **kwargs):
        if argv[0] == 'ssh':
            return subprocess.CompletedProcess(argv, 0, json.dumps(exchange([])))
        calls.append(json.loads(kwargs['input']))
    monkeypatch.setattr(subprocess, 'run', run)
    event = dict(rule='run-silent', run_id='fixture', kind='completion', episode=100)
    for change in ({}, {'cancelled': True}, {'episode': 200}):
        deliver_event(tmp_path, dict(event, **change))
        poll(['fixture'], 'notify', tmp_path)
    assert len(calls) == len({e['event_id'] for e in calls}) == 3
