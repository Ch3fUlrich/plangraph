import json
import sys

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
