"""D-3465: a leg probe must never commit into the caller's worktree.

The measured leak: a writer-leg probe committed ok.txt ("probe") into the
CALLER's current worktree.  GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE inherited
from a caller inside a lane worktree (or a git hook) redirect every child
`git` to the caller's repo regardless of the probe's temp-dir cwd, so the
probe's GIT_ISOLATION (GIT_CONFIG_* only) was not isolation at all.  Pins:
the probe env strips the git redirection vars for every harness child and
for the probe's own git calls, probe() verifies after the run that the
caller's cwd-repo HEAD is unchanged (reporting "probe leaked into caller
repo" without ever auto-resetting the caller repo), and every harness
client is launched with an explicit cwd.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from plangraph.routing_support.leg_state import LegStateStore

GIT_REDIRECT = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY')


def _env():
    """The env a git call sees when no redirection is exported."""
    env = dict(os.environ)
    for name in GIT_REDIRECT:
        env.pop(name, None)
    return env


def _git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True, env=_env()).stdout.strip()


def _caller_repo(tmp_path):
    """A lane worktree: a plain repo the probe process runs inside."""
    repo = tmp_path / 'lane'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True,
                   capture_output=True, env=_env())
    _git(repo, 'config', 'user.name', 'Lane')
    _git(repo, 'config', 'user.email', 'lane@example.invalid')
    _git(repo, 'config', 'commit.gpgsign', 'false')
    (repo / 'f').write_text('v1')
    _git(repo, 'add', 'f')
    _git(repo, 'commit', '-qm', 'initial')
    return repo


def _leg():
    return SimpleNamespace(model='gemini-3.8-flash', harness='agy')


# The measured incident's writer: asked to create and commit ok.txt, a real
# writer resolves the repo with `git rev-parse --show-toplevel`, so a hijacked
# repo resolution makes it commit into the CALLER's worktree.
WRITER = (
    "import json, subprocess, sys\n"
    "from pathlib import Path\n"
    "top = subprocess.check_output(['git', 'rev-parse', '--show-toplevel']).decode().strip()\n"
    "Path(top, 'ok.txt').write_text('ok')\n"
    "subprocess.run(['git', '-C', top, 'add', 'ok.txt'], check=True)\n"
    "subprocess.run(['git', '-C', top, 'commit', '-m', 'probe'], check=True)\n"
    "Path(sys.argv[1]).write_text(json.dumps(dict(top=top,\n"
    "    head=subprocess.check_output(['git', '-C', top, 'rev-parse', 'HEAD']).decode().strip(),\n"
    "    subject=subprocess.check_output(['git', '-C', top, 'log', '-1', '--format=%s']).decode().strip())))\n"
)


def test_probe_in_lane_worktree_commits_only_into_the_temp_repo(tmp_path, monkeypatch):
    from plangraph.spawner import leg_probe
    from plangraph.spawner.runner import SpawnerRunner

    caller = _caller_repo(tmp_path)
    monkeypatch.chdir(caller)
    # the incident topology: the caller exports its worktree to every git call
    monkeypatch.setenv('GIT_DIR', str(caller / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(caller))
    head_before = _git(caller, 'rev-parse', 'HEAD')
    evidence = tmp_path / 'evidence.json'
    captured = {}

    async def factory(*args, **kwargs):
        captured.update(kwargs)
        return await asyncio.create_subprocess_exec(
            sys.executable, '-c', WRITER, str(evidence), **kwargs)

    runner = SpawnerRunner(process_factory=factory)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    result = asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=30, store=store))

    assert result['ok'] is True, result
    # the caller's repo is untouched: same HEAD, clean tree, no ok.txt
    assert _git(caller, 'rev-parse', 'HEAD') == head_before
    assert _git(caller, 'status', '--porcelain') == ''
    assert not (caller / 'ok.txt').exists()
    # the temp repo got the commit (top level is the probe's temp dir)
    seen = json.loads(evidence.read_text())
    assert seen['top'] == captured['cwd']
    assert Path(captured['cwd']).name.startswith('plangraph-leg-probe-')
    assert seen['subject'] == 'probe'
    assert seen['head'] != head_before
    # the child env carries the config isolation but never git redirection
    for name in GIT_REDIRECT:
        assert name not in captured['env']
    assert captured['env']['GIT_CONFIG_GLOBAL'] == os.devnull
    assert captured['env']['GIT_CONFIG_NOSYSTEM'] == '1'


def test_probe_reports_leak_when_the_writer_commits_into_the_caller_repo(tmp_path, monkeypatch):
    from plangraph.spawner import leg_probe
    from plangraph.spawner.runner import SpawnerRunner

    caller = _caller_repo(tmp_path)
    monkeypatch.chdir(caller)
    monkeypatch.setenv('GIT_DIR', str(caller / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(caller))
    # a writer that commits into the caller's repo no matter what (a harness
    # falling back to a configured workspace, or re-exporting GIT_DIR)
    leak = (
        "import subprocess\n"
        "from pathlib import Path\n"
        "Path({repo!r}, 'ok.txt').write_text('ok')\n"
        "subprocess.run(['git', '-C', {repo!r}, 'add', 'ok.txt'], check=True)\n"
        "subprocess.run(['git', '-C', {repo!r}, 'commit', '-m', 'probe'], check=True)\n"
    ).format(repo=str(caller))

    async def factory(*args, **kwargs):
        return await asyncio.create_subprocess_exec(sys.executable, '-c', leak, **kwargs)

    runner = SpawnerRunner(process_factory=factory)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    result = asyncio.run(leg_probe.probe(runner, _leg(), timeout_s=30, store=store))

    assert result['ok'] is False
    assert result['reason'] == 'probe leaked into caller repo'
    # never auto-reset the caller repo: the leaked commit is still HEAD
    assert _git(caller, 'log', '-1', '--format=%s') == 'probe'
    assert (caller / 'ok.txt').exists()


@pytest.mark.parametrize('client, binary, model', [
    ('opencode', 'opencode', 'omniroute/cx-gpt-6-luna'),
    ('agy', 'agy', 'gemini-3.8-flash'),
    ('qodercli', 'qodercli', 'gemini-3.8-flash'),
])
def test_every_harness_client_is_launched_with_the_explicit_cwd(
        tmp_path, monkeypatch, client, binary, model):
    from plangraph.spawner import resolver
    from plangraph.spawner.runner import SpawnerRunner

    async def served(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', served)

    captured = {}

    async def factory(*args, **kwargs):
        captured['argv'], captured['kwargs'] = args, kwargs

        async def communicate():
            return b'', b''
        return SimpleNamespace(pid=123, returncode=0, communicate=communicate)

    lane = tmp_path / 'lane'
    lane.mkdir()
    monkeypatch.setenv('PWD', str(tmp_path))   # the caller's shell dir (D-3505)
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)

    async def run():
        job = await runner.spawn('probe prompt', model_or_combo=model, role='implement',
                                client=client, cwd=str(lane))
        await runner._tasks[job.job_id]
        return job

    asyncio.run(run())
    # the harness's own command, started in the explicit cwd -- never a
    # fallback to the parent process cwd or a configured workspace
    assert Path(captured['argv'][0]).name == binary
    assert captured['kwargs']['cwd'] == str(lane)
    assert captured['kwargs']['env']['PWD'] == str(lane)   # D-3505: never the caller's PWD
