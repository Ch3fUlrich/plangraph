"""Regression coverage for the first split CI review."""
import importlib
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('job', ['pytest', 'pytest-postgres'])
def test_unsharded_collection_check_rejects_empty_suite(tmp_path, job):
    workflow = yaml.safe_load((ROOT / '.github/workflows/tests.yml').read_text())
    step = next((s for s in workflow['jobs'][job]['steps']
                 if s.get('name') == 'Check nonempty collection'), None)
    assert step is not None
    assert step['if'] == 'matrix.shard == 0'
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_hidden.py').write_text('# keeper_util\ndef test_hidden(): pass\n')
    (tmp_path / 'tests/conftest.py').write_text(
        "def pytest_collection_modifyitems(items): items.clear()\n")
    if job == 'pytest-postgres':
        import re
        for name in set(re.findall(r'tests/[a-zA-Z0-9_/.]+', step['run'])):
            path = tmp_path / name
            if name.endswith('.py'):
                path.write_text('')
            else:
                path.mkdir(exist_ok=True)
        steps = workflow['jobs'][job]['steps']
        run = next(s['run'] for s in steps if s.get('name', '').startswith('pytest'))
        # Both PR and push guards must collect the exact Postgres execution selection.
        within = run.split('--within ', 1)[1].split(' --run', 1)[0]
        assert '--within ' + within + ' --run --collect-only' in step['run']
        assert '--collect-only ' + within in step['run']
        assert step['run'].split('if [', 1)[0].strip() in run
    command = step['run'].replace('${{ github.event_name }}', 'push')
    env = dict(os.environ, PYTEST_SHARD='0/3')
    result = subprocess.run(['bash', '-e', '-c', command], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 5, result.stdout + result.stderr


def test_registry_guard_detects_added_worktree(tmp_path, config_guard):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.invalid', 'commit', '-qm',
                    'initial', '--allow-empty'], check=True)
    with pytest.raises(AssertionError, match='worktree registry changed'):
        with config_guard(repo):
            subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach',
                            str(tmp_path / 'added')], check=True, capture_output=True)


@pytest.mark.parametrize('name', ['test_split_ci_test_affected_tests',
                                'test_split_ci_test_preflight', 'test_split_ci_hook'])
def test_split_modules_do_not_recollect_imported_tests(name):
    # Later split parts fold these wrappers into their original modules.
    if not (ROOT / 'tests' / (name + '.py')).exists():
        name = {'test_split_ci_test_affected_tests': 'test_affected_tests',
                'test_split_ci_test_preflight': 'test_preflight',
                'test_split_ci_hook': 'test_hooks_prepush'}[name]
    module = importlib.import_module(name)
    duplicates = [key for key, value in vars(module).items()
                  if key.startswith('test_') and callable(value)
                  and value.__module__ != module.__name__]
    assert duplicates == []


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process group regression')
def test_preflight_timeout_kills_pytest_descendants(tmp_path, monkeypatch):
    from test_preflight import preflight
    script = tmp_path / 'affected_tests.py'
    pidfile = tmp_path / 'child.pid'
    script.write_text('import subprocess, sys, time\n'
                      'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
                      f'open({str(pidfile)!r}, "w").write(str(child.pid))\n'
                      'time.sleep(30)\n')
    monkeypatch.setattr(preflight, 'TEST_TIMEOUT', 1)
    pid = None
    try:
        code, _ = preflight.run([sys.executable, str(script)], tmp_path, dict(os.environ))
        assert code == 1
        pid = int(pidfile.read_text())
        # A killed orphan can briefly remain a zombie until init reaps it.
        for _ in range(100):
            status = Path(f'/proc/{pid}/stat')
            if not status.exists() or status.read_text().split()[2] == 'Z':
                break
            time.sleep(.01)
        else:
            pytest.fail('pytest descendant survived the preflight timeout')
    finally:
        if pid is None and pidfile.exists():
            pid = int(pidfile.read_text())
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('ref,expected', [('refs/heads/main', 1), ('refs/heads/topic', 0), ('refs/tags/v1', 0)])
def test_missing_preflight_refuses_only_main(tmp_path, monkeypatch, capsys, ref, expected):
    hook = runpy.run_path(str(ROOT / 'tools/hooks/pre-push'))
    scope = hook['push_main'].__globals__
    from types import SimpleNamespace
    monkeypatch.setattr(sys, 'stdin', __import__('io').StringIO(
        f"{ref} {'1' * 40} {ref} {'0' * 40}\n"))
    monkeypatch.delenv('PREFLIGHT_SKIP', raising=False)
    monkeypatch.setitem(scope, 'git_out', lambda *a: str(tmp_path) if a[0] == 'rev-parse' else '')
    monkeypatch.setitem(scope, 'file_size_gate', lambda: 0)
    monkeypatch.setitem(scope, 'base_commit', lambda *a: 'base')
    monkeypatch.setitem(scope, 'main_baselines', lambda *a: [])
    monkeypatch.setitem(scope, 'changelog_docs_warn', lambda *a: None)
    monkeypatch.setitem(scope, 'git', lambda *a: SimpleNamespace(returncode=0))
    assert hook['push_main']() == expected
    assert ('tools/preflight.py missing: push refused' in capsys.readouterr().err) == bool(expected)


@pytest.mark.parametrize('ignored', [False, True])
def test_affected_collection_is_unsharded_and_does_not_execute(tmp_path, monkeypatch, ignored):
    from test_affected_tests import selector
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_hidden.py').write_text('def test_hidden(): assert False\n')
    (tmp_path / 'tests/conftest.py').write_text(
        "import os\nassert not os.environ.get('PYTEST_SHARD')\n"
        + ("collect_ignore_glob = ['test_*.py']\n" if ignored else ''))
    monkeypatch.setenv('PYTEST_SHARD', '0/3')
    monkeypatch.setattr(sys, 'argv', ['affected_tests.py', '--base', 'HEAD', '--changed',
                                    'tests/conftest.py', '--run', '--collect-only'])
    assert selector().main(root=tmp_path) == (5 if ignored else 0)


def test_empty_shard_still_succeeds(monkeypatch, request):
    from pathlib import Path
    from types import SimpleNamespace
    # Several directories hold a conftest.py; `import conftest` names whichever was loaded last.
    conftest = request.config.pluginmanager.getplugin(str(Path(__file__).with_name('conftest.py')))
    monkeypatch.setenv('PYTEST_SHARD', '0/3')
    session = SimpleNamespace(exitstatus=pytest.ExitCode.NO_TESTS_COLLECTED)
    conftest.pytest_sessionfinish(session, session.exitstatus)
    assert session.exitstatus == pytest.ExitCode.OK
