import asyncio
import os
import subprocess
from types import SimpleNamespace

from plangraph.spawner.env_redact import first_reason, harness_limit_line
from plangraph.spawner.git_guard import writer_launch
from plangraph.spawner.exit_outcome import fallthrough_reason
from plangraph.routing_support.leg_state import LegStateStore


QUOTA = 'error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 10h40m26s.'


def test_quota_state_and_compound_reset(tmp_path, monkeypatch):
    monkeypatch.setattr('plangraph.routing_support.leg_state.time.time', lambda: 1000)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    job = SimpleNamespace(exit_code=3, stdout='', stderr='\x1b[0m\n' + QUOTA)
    record = {}
    reason = fallthrough_reason(SimpleNamespace(leg_reader=store),
                                SimpleNamespace(model='agy/model'), job, record)
    assert reason == 'harness_limit: ' + QUOTA
    assert record['reason'] == 'harness_limit'
    state = store.leg_status('agy/model', now=1000)
    assert state['state'] == 'limited'
    assert state['evidence']['reset_at'] == 1000 + 38426
    store.record_harness_limit('agy/other', 'Resets in 2h 5m', 'test', now=1000)
    assert store.leg_status('agy/other', now=1000)['evidence']['reset_at'] == 8500
    assert first_reason('\x1b[0m\n\nactual error') == 'actual error'
    assert harness_limit_line(0, '', QUOTA) is None


def test_writer_identity_commits_with_host_committer(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    for key, value in [('user.name', 'Host'), ('user.email', 'host@example.com')]:
        subprocess.run(['git', '-C', str(tmp_path), 'config', key, value], check=True)
    prompt, extra = writer_launch('work', 'implement', 'agy/model', 'run-123')
    assert prompt.endswith('Writer: agy/model run-123')

    async def fake_writer():
        process = await asyncio.create_subprocess_exec(
            'git', 'commit', '--allow-empty', '-m', 'x', cwd=tmp_path,
            env={**os.environ, **extra}, stdout=asyncio.subprocess.PIPE)
        await process.communicate()
        assert process.returncode == 0

    asyncio.run(fake_writer())
    identity = subprocess.check_output(['git', 'log', '-1', '--format=%an|%ae|%cn'], cwd=tmp_path).decode().strip()
    assert identity == 'agy/model (plangraph writer)|run-123@writer.plangraph.invalid|Host'
    assert writer_launch('review', 'review', 'agy/model', 'run-123') == ('review', {})
