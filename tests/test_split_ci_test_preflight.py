from pathlib import Path
import shutil
import subprocess
import pytest
from test_preflight import preflight, repo, fake_runner

@pytest.mark.parametrize("command,family", [
    (["cargo", "clippy"], "rust"), (["cargo", "test"], "rust"),
    (["python3", "affected_tests.py"], "tests")])
def test_slow_passing_check(repo, monkeypatch, capsys, command, family):
    (repo / "rust").mkdir()
    # Scale seconds by 1/1000: the fake command outlives the old 60 s limit.
    monkeypatch.setattr(preflight, "CHECK_TIMEOUT", 0.06)
    monkeypatch.setattr(preflight, "RUST_TIMEOUT", 0.6)
    monkeypatch.setattr(preflight, "TEST_TIMEOUT", 0.9)
    real_subprocess = subprocess.run
    original = preflight.run

    def sleeping_check(argv, **kwargs):
        import sys
        assert kwargs["timeout"] == (0.6 if family == "rust" else 0.9)
        # Rust checks run from repo/rust, so a relative interpreter path breaks.
        return real_subprocess([str(Path(sys.executable).resolve()), "-c",
                                "import time; time.sleep(0.12)"],
                               **kwargs)

    def run(argv, cwd, env):
        if argv[:2] == command or (family == "tests" and
                                  Path(argv[1]).name == "affected_tests.py"):
            return original(command, cwd, env)
        return 0, ""

    monkeypatch.setattr(preflight.subprocess, "run", sleeping_check)
    monkeypatch.setattr(preflight, "run_test_process", sleeping_check)
    monkeypatch.setattr(preflight, "run", run)
    monkeypatch.setattr(preflight, "test_environment", lambda root, env: env)
    monkeypatch.setattr(preflight, "test_baseline", lambda root, base: base)
    monkeypatch.setattr(preflight, "rust_commands",
                        lambda *args: [command] if family == "rust" else [])
    assert preflight.main(["--base", "HEAD"], repo) == 0
    assert f"PASS {family} " in capsys.readouterr().out


def test_missing_rust_baseline(repo, monkeypatch, capsys):
    fake_runner(monkeypatch)
    assert preflight.main(["--base", "missing", "--fast"], repo) == 0
    assert "rust: SKIP: revision baseline unavailable" in capsys.readouterr().out


def test_summary_is_immediate(repo, monkeypatch, capsys):
    def run(command, cwd, env):
        if Path(command[1]).name == "check_file_size.py":
            assert "SKIP scrub " in capsys.readouterr().out
        return 0, ""

    monkeypatch.setattr(preflight, "run", run)
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0


@pytest.mark.parametrize('source', ['venv', 'configured', 'path'])
def test_python_interpreter_order(tmp_path, monkeypatch, source):
    import runpy
    configured = tmp_path / 'configured-python'
    monkeypatch.setenv('PREFLIGHT_PYTHON', str(configured))
    env_python = tmp_path / '.venv/bin/python'
    if source == 'venv':
        env_python.parent.mkdir(parents=True)
        env_python.touch()
    elif source == 'path':
        monkeypatch.delenv('PREFLIGHT_PYTHON')
    expected = str(env_python if source == 'venv' else configured) if source != 'path' else shutil.which('python3') or 'python3'
    hook = runpy.run_path(str(Path(preflight.__file__).with_name('hooks') / 'pre-push'))
    assert preflight.python_interpreter(tmp_path) == expected
    assert hook['python_interpreter'](tmp_path) == expected
    calls = fake_runner(monkeypatch)
    assert preflight.main(['--base', 'HEAD', '--fast'], tmp_path) == 0
    assert all(command[0] == expected for command, _, _ in calls)


def test_shared_manifest_selects_only_workspace_members(repo, monkeypatch):
    rust = repo / 'rust'
    for directory, name in [('one', 'one'), ('worker', 'pg-worker')]:
        crate = rust / directory
        crate.mkdir(parents=True)
        (crate / 'Cargo.toml').write_text('[package]\nname = "' + name + '"\n')
    (rust / 'Cargo.toml').write_text('[workspace]\nmembers = ["one"]\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'workspace'], cwd=repo, check=True)
    monkeypatch.setattr(preflight.shutil, 'which', lambda _: '/fake/cargo')
    commands = preflight.rust_commands(repo, 'HEAD~1', 'HEAD')
    assert commands[2] == ['cargo', 'test', '-p', 'one']

def test_local_tests_use_main_while_policy_keeps_push_base(repo, monkeypatch):
    subprocess.run(['git', 'update-ref', 'refs/remotes/origin/main', 'HEAD'], cwd=repo, check=True)
    calls = fake_runner(monkeypatch)
    assert preflight.main(['--base', 'HEAD'], repo) == 0
    tests = next(cmd for cmd, _, _ in calls if Path(cmd[1]).name == 'affected_tests.py')
    assert tests[tests.index('--base') + 1] == 'origin/main'
    assert '--local' in tests and '--files-only' in tests
    assert all(env['FILESIZE_REQUIRE_BASE'] == 'origin/main' for cmd, _, env in calls
               if Path(cmd[1]).name == 'check_file_size.py')
