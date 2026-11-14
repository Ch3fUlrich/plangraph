"""Pre-push Git context must not override independent test repositories."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from test_preflight import fake_runner, preflight, repo

FIXTURE_EMAIL = '@'.join(('123+fixture', 'users.noreply.github.com'))


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
                    "user.email=" + FIXTURE_EMAIL, "commit", "-q",
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
