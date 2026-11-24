"""A Codex writer probe runs where its commit can land; a sandbox refusal is a probe defect."""
import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

from plangraph.routing_support.clients import build_command
from plangraph.routing_support.leg_state import LegStateStore


def _leg():
    return SimpleNamespace(id='cx', model='gpt-6.2-sol-high', harness='codex', roles=('implement',))


def test_codex_writer_command_is_workspace_write_with_the_git_dir(monkeypatch):
    monkeypatch.setattr('plangraph.routing_support.clients.resolve_codex_client', lambda *a, **k: ('codex', None))
    cmd = build_command('p', 'gpt-6.2-sol-high', role='implement', client='codex', writable_dirs=['/r/.git'])
    assert cmd[cmd.index('--sandbox') + 1] == 'workspace-write'
    assert cmd[cmd.index('--add-dir') + 1] == '/r/.git'
    assert cmd[-1] == '-'
    plain = build_command('p', 'gpt-6.2-sol-high', role='implement', client='codex')
    assert '--sandbox' not in plain and '--add-dir' not in plain


def _probe(tmp_path, monkeypatch, spawn):
    from plangraph.spawner import leg_probe
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **k: '/bin/codex')
    store = LegStateStore(tmp_path / 'legs.sqlite')

    async def cancel(job_id):
        pass
    runner = SimpleNamespace(spawn=spawn, cancel=cancel)
    return asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=5, store=store)), store


def test_writer_probe_of_a_codex_leg_grants_the_probe_git_dir(tmp_path, monkeypatch):
    seen = {}

    async def spawn(**kw):
        seen.update(kw)
        root = Path(kw['cwd'])
        (root / 'ok.txt').write_text('ok')
        env = {'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'PATH': '/usr/bin:/bin'}
        subprocess.run(['git', '-C', str(root), 'add', 'ok.txt'], check=True, env=env)
        subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'probe'], check=True, env=env)
        return SimpleNamespace(job_id='j', status='completed', exit_code=0, stdout='', stderr='')
    result, _ = _probe(tmp_path, monkeypatch, spawn)
    assert result['ok'], result
    assert seen['writable_dirs'] == [str(Path(seen['cwd']) / '.git')]


def test_sandbox_refusal_is_a_probe_defect_that_books_nothing(tmp_path, monkeypatch):
    async def spawn(**kw):
        (Path(kw['cwd']) / 'ok.txt').write_text('ok')
        return SimpleNamespace(job_id='j', status='completed', exit_code=0, stdout='OK',
                               stderr='fatal: Unable to create .git/index.lock: Read-only file system')
    result, store = _probe(tmp_path, monkeypatch, spawn)
    assert result['ok'] is False and result['infra'] is True, result
    assert 'sandbox' in result['reason']
    assert store.leg_status('gpt-6.2-sol-high')['state'] != 'down'


def test_a_real_no_commit_still_books_the_leg_down(tmp_path, monkeypatch):
    async def spawn(**kw):
        return SimpleNamespace(job_id='j', status='completed', exit_code=0, stdout='done', stderr='')
    result, _ = _probe(tmp_path, monkeypatch, spawn)
    assert result['ok'] is False and not result['infra'] and result['reason'].startswith('no commit')
