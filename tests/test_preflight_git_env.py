"""Pre-push Git context must not override independent test repositories."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from test_preflight import fake_runner, preflight, repo


@pytest.mark.parametrize("refuse", [False, True])
def test_nested_fixture_keeps_its_installed_hook(repo, monkeypatch, refuse):
    caller_hooks = repo / "caller-hooks"
    caller_hooks.mkdir()
    capture = repo / "capture.py"
    captured = repo / "git-context.json"
    capture.write_text(
        "import json, os\nfrom pathlib import Path\n"
        f"Path({str(captured)!r}).write_text(json.dumps("
        "{k: v for k, v in os.environ.items() if k.startswith('GIT_')}))\n")
    wrapper = caller_hooks / "pre-push"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.join([
        Path(sys.executable).as_posix(), capture.as_posix()]) + "\n")
    wrapper.chmod(0o755)
    caller_remote = repo / "caller.git"
    subprocess.run(["git", "init", "-q", "--bare", str(caller_remote)], check=True)
    caller_env = dict(os.environ, GIT_CONFIG_COUNT="1",
                      GIT_CONFIG_KEY_0="preflight.fixture", GIT_CONFIG_VALUE_0="caller",
                      GIT_INDEX_FILE=str(repo / ".git/index"))
    subprocess.run(["git", f"--git-dir={repo / '.git'}", f"--work-tree={repo}",
                    "-c", f"core.hooksPath={caller_hooks}", "push", "-q",
                    str(caller_remote), "HEAD:refs/heads/main"],
                   cwd=repo, env=caller_env, check=True)
    context = json.loads(captured.read_text())
    assert {"GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
            "GIT_PREFIX"} <= context.keys()

    nested = repo / "independent-checkout"
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-q",
                    "--allow-empty", "-m", "fixture"], cwd=nested, check=True)
    nested_remote = repo / "nested.git"
    subprocess.run(["git", "init", "-q", "--bare", str(nested_remote)], check=True)
    hooks = nested / "fixture-hooks"
    hooks.mkdir()
    marker = nested / "hook-invoked"
    hook = hooks / "pre-push"
    hook.write_text("#!/bin/sh\ntouch " + shlex.quote(marker.as_posix())
                    + f"\nexit {int(refuse)}\n")
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.hooksPath", str(hooks)],
                   cwd=nested, check=True)
    for key, value in context.items():
        monkeypatch.setenv(key, value)
    original = preflight.run
    policy, test_envs = [], []

    def run(command, cwd, env):
        name = Path(command[1]).name
        if name == "check_file_size.py":
            assert env["FILESIZE_REQUIRE_BASE"] == "HEAD"
            assert all(env[key] == value for key, value in context.items())
            policy.append(name)
        if name == "affected_tests.py":
            test_envs.append(env)
            return original(["git", "push", "-q", str(nested_remote),
                             "HEAD:refs/heads/main"], nested, env)
        return 0, ""

    monkeypatch.setattr(preflight, "run", run)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [])
    assert preflight.main(["--base", "HEAD"], repo) == int(refuse)
    assert policy and marker.exists(), "the fixture's installed hook must execute"
    assert not {"GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                "GIT_CONFIG_VALUE_0", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_PREFIX"}.intersection(test_envs[0])


def test_missing_git_environment_discovery_refuses_tests(repo, monkeypatch, capsys):
    calls = fake_runner(monkeypatch)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [])

    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(preflight.subprocess, "check_output", unavailable)
    assert preflight.main(["--base", "HEAD"], repo) == 1
    assert "FAIL tests " in capsys.readouterr().out
    assert not any(Path(command[1]).name == "affected_tests.py"
                   for command, _, _ in calls)


def test_preflight_restores_user_cargo_path(repo, monkeypatch):
    cargo_bin = repo / '.cargo/bin'
    cargo_bin.mkdir(parents=True)
    monkeypatch.setenv('PATH', '/usr/bin')
    calls = fake_runner(monkeypatch)
    assert preflight.main(['--base', 'HEAD', '--fast'], repo) == 0
    assert calls[0][2]['PATH'].split(os.pathsep)[0] == str(cargo_bin)


def test_hook_restores_user_cargo_path(repo, monkeypatch):
    import io
    import runpy
    cargo_bin = repo / '.cargo/bin'
    cargo_bin.mkdir(parents=True)
    monkeypatch.setenv('PATH', '/usr/bin')
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    hook = runpy.run_path(str(Path(preflight.__file__).with_name('hooks') / 'pre-push'))
    assert hook['push_main']() == 0
    assert os.environ['PATH'].split(os.pathsep)[0] == str(cargo_bin)


def test_missing_cargo_stops_selected_placement_tests(repo, monkeypatch, capsys):
    from test_affected_tests import selector
    from types import SimpleNamespace
    test = repo / 'tests/test_placement_example.py'
    test.parent.mkdir()
    test.write_text('def test_example(): pass\n')
    monkeypatch.setenv('PATH', str(repo / 'empty-bin'))
    module = selector()
    calls = []
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(sys, 'argv', ['affected_tests', '--base', 'HEAD', '--changed',
                                   'tests/test_placement_example.py', '--run'])
    assert module.main(root=repo) == 1
    assert not calls
    assert capsys.readouterr().err.splitlines() == ['CARGO FAIL: cargo is required for selected placement tests']


def test_preflight_keeps_scrub_base_separate_from_affected_base(repo, monkeypatch):
    calls = fake_runner(monkeypatch)
    assert preflight.main(['--base', 'HEAD', '--scrub-base', 'scrub-start', '--fast'], repo) == 0
    command = calls[0][0]
    assert command[command.index('--base') + 1] == 'scrub-start'
    policy = next(command for command, _, _ in calls if Path(command[1]).name == 'ci_commit_gate.py')
    assert policy[policy.index('--base') + 1] == 'HEAD'


def test_rust_checks_receive_no_hook_git_environment(repo, monkeypatch):
    context = dict(GIT_DIR=str(repo / '.git'), GIT_WORK_TREE=str(repo),
                   GIT_INDEX_FILE=str(repo / '.git/index'), GIT_CONFIG_COUNT='1',
                   GIT_CONFIG_KEY_0='core.bare', GIT_CONFIG_VALUE_0='false',
                   GIT_FUTURE_HOOK_SENTINEL='must not reach rust')
    for key, value in context.items():
        monkeypatch.setenv(key, value)
    calls = fake_runner(monkeypatch)
    monkeypatch.setattr(preflight, 'rust_commands', lambda *a: [['cargo', 'test', '-p', 'fixture']])
    assert preflight.main(['--base', 'HEAD', '--fast'], repo) == 0
    rust_env = next(env for command, _, env in calls if command[0] == 'cargo')
    assert not any(key.upper().startswith('GIT_') for key in rust_env)


def _git_snapshot(repo):
    metadata = repo / '.git'
    return {p.relative_to(metadata).as_posix(): p.read_bytes()
            for p in metadata.rglob('*') if p.is_file()}



@pytest.mark.parametrize('boundary', ['hook', 'selector', 'pytest'])
def test_hook_binding_never_mutates_caller_repository(tmp_path, boundary):
    """Every repository and every deliberately leaked variable belongs to this fixture."""
    import shutil
    source = Path(__file__).resolve().parents[1]
    victim, suite, checkout = (tmp_path / name for name in ('caller', 'suite', 'checkout'))
    victim.mkdir()
    suite.mkdir()
    checkout.mkdir()
    home = tmp_path / 'home'
    home.mkdir()
    clean = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
    clean.update(HOME=str(home), GIT_CONFIG_NOSYSTEM='1')
    subprocess.run(['git', 'init', '-qb', 'protected', str(victim)], env=clean, check=True)
    subprocess.run(['git', '-C', str(victim), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'original'],
                   env=clean, check=True)
    before = _git_snapshot(victim)
    metadata = victim / '.git'
    incoming = dict(clean, GIT_DIR=str(metadata), GIT_WORK_TREE=str(victim),
                    GIT_COMMON_DIR=str(metadata), GIT_INDEX_FILE=str(metadata / 'index'),
                    GIT_OBJECT_DIRECTORY=str(metadata / 'objects'),
                    GIT_ALTERNATE_OBJECT_DIRECTORIES=str(metadata / 'objects'),
                    GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='fixture.inherited',
                    GIT_CONFIG_VALUE_0='caller', GIT_PREFIX='caller/',
                    FIXTURE_CHECKOUT=str(checkout))
    incoming['PYTHONPATH'] = os.pathsep.join((str(source / 'src'), str(suite / 'tests')))
    incoming.pop('PYTEST_ADDOPTS', None)
    incoming.pop('PYTEST_SHARD', None)
    mutation = (
        "import os, subprocess\n"
        "def test_fixture():\n"
        "    os.chdir(os.environ['FIXTURE_CHECKOUT'])\n"
        "    subprocess.run(['git', 'init', '-qb', 'main'], check=True)\n"
        "    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', "
        "'commit', '--allow-empty', '-qm', 'fixture mutation'], check=True)\n"
        "    subprocess.run(['git', 'config', 'user.name', 'Child fixture'], check=True)\n"
    )
    if boundary == 'hook':
        tools = suite / 'tools'
        tools.mkdir()
        (tools / 'affected_tests.py').write_text(mutation + '\ntest_fixture()\n')
        driver = suite / 'driver.py'
        driver.write_text(
            'import runpy\nfrom pathlib import Path\n'
            f'hook = runpy.run_path({str(source / "tools/hooks/pre-push")!r})\n'
            f'raise SystemExit(hook["affected_gate"](Path({str(suite)!r}), "unused", "HEAD"))\n')
        command = [sys.executable, str(driver)]
    else:
        tests = suite / 'tests'
        tests.mkdir()
        (tests / 'test_binding.py').write_text(mutation)
        if boundary == 'selector':
            command = [sys.executable, str(source / 'tools/affected_tests.py'), '--root', str(suite),
                       '--base', 'HEAD', '--changed', 'tests/test_binding.py', '--run', '--workers', '0']
        else:
            for name in ('conftest.py', 'pgtest_shm.py', '_keeper_threads.py', 'win_skips.py'):
                shutil.copy2(source / 'tests' / name, tests / name)
            command = [sys.executable, '-m', 'pytest', '-q', 'tests/test_binding.py']
    result = subprocess.run(command, cwd=suite, env=incoming, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git_snapshot(victim) == before, 'caller refs, HEAD or config changed'
    assert (checkout / '.git/HEAD').is_file()


@pytest.mark.parametrize('boundary', ['hook', 'selector', 'pytest'])
def test_test_environment_uses_git_discovery_and_removes_config_prefix(tmp_path, monkeypatch, boundary):
    import conftest
    import runpy
    from test_affected_tests import selector
    root = Path(__file__).resolve().parents[1]
    helper = (runpy.run_path(str(root / 'tools/hooks/pre-push'))['test_environment'] if boundary == 'hook'
              else selector().test_environment if boundary == 'selector' else conftest._test_environment)
    inherited = dict(GIT_DIR='caller', GIT_FUTURE_BINDING='caller', GIT_PREFIX='prefix/',
                     GIT_CONFIG='caller', GIT_CONFIG_GLOBAL='caller', GIT_CONFIG_NOSYSTEM='1',
                     GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_7='caller', GIT_CONFIG_VALUE_7='caller',
                     git_config_future='caller', PATH='preserved', GIT_SSH_COMMAND='preserved')
    before = dict(inherited)
    def discover(command, **kwargs):
        assert command == ['git', 'rev-parse', '--local-env-vars']
        assert kwargs['env'] == inherited
        return 'GIT_DIR\nGIT_FUTURE_BINDING\n'
    monkeypatch.setattr(subprocess, 'check_output', discover)
    assert helper(tmp_path, inherited) == {'PATH': 'preserved', 'GIT_SSH_COMMAND': 'preserved'}
    assert inherited == before  # Hook-owned Git calls retain their original context.


@pytest.mark.parametrize('boundary', ['hook', 'selector'])
def test_git_discovery_failure_never_launches_tests(tmp_path, monkeypatch, boundary):
    import runpy
    from test_affected_tests import selector
    (tmp_path / 'tools').mkdir()
    (tmp_path / 'tools/affected_tests.py').touch()
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_fixture.py').touch()
    monkeypatch.setenv('PREFLIGHT_SKIP', '1')
    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(subprocess, 'check_output', unavailable)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('test process launched'))
    if boundary == 'hook':
        hook = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tools/hooks/pre-push'))
        assert hook['affected_gate'](tmp_path, 'base', 'head') == 1
    else:
        module = selector()
        monkeypatch.setattr(module, 'select_tests', lambda *a, **k: {'tests': ['tests/test_fixture.py']})
        monkeypatch.setattr(sys, 'argv', ['affected_tests', '--base', 'HEAD', '--changed',
                                       'tests/test_fixture.py', '--run'])
        assert module.main(root=tmp_path) == 1
