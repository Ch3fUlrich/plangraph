"""Hermetic preflight orchestration, without real policy tools or cargo."""
import importlib.util
from pathlib import Path
import re
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).resolve().parents[1] / "tools/preflight.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-q",
                    "--allow-empty", "-m", "initial"], check=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PLANGRAPH_SCRUB_WORDS", raising=False)
    return tmp_path


def fake_runner(monkeypatch, failure=None, output="policy rejected\n"):
    calls = []

    def run(command, cwd, env):
        calls.append((command, cwd, env))
        name = Path(command[1]).name if command[0] != "cargo" else "cargo"
        return (1, output) if name == failure else (0, "")

    monkeypatch.setattr(preflight, "run", run)
    return calls


@pytest.mark.parametrize("family,tool", [
    ("scrub", "scrub_check.py"), ("policy", "check_file_size.py"),
    ("policy", "check_new_code_lang.py"), ("policy", "check_changelog_docs.py"),
    ("policy", "ci_commit_gate.py"), ("rust", "cargo"),
    ("tests", "affected_tests.py")])
def test_failure(repo, monkeypatch, capsys, family, tool):
    fake_runner(monkeypatch, tool)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [["cargo", "fmt", "--check"]])
    assert preflight.main(["--base", "HEAD"], repo) == 1
    assert re.search(rf"^FAIL {family} \d+\.\d{{3}}$", capsys.readouterr().out, re.M)


def test_infra_retry(repo, monkeypatch, capsys):
    calls = fake_runner(monkeypatch, "scrub_check.py", "HTTP 503 download failed\n")
    assert preflight.main(["--base", "HEAD"], repo) == 3
    assert len(calls) == 7
    assert "INFRA scrub " in capsys.readouterr().out


def test_fast_summary_and_ci_env(repo, monkeypatch, capsys):
    calls = fake_runner(monkeypatch)
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[:2] for line in lines] == [
        ["PASS", "scrub"], ["PASS", "policy"], ["SKIP", "rust"], ["SKIP", "tests"]]
    assert all(re.fullmatch(r"(?:PASS|SKIP) \w+ \d+\.\d{3}", line) for line in lines)
    assert len(calls) == 5
    assert calls[1][2]["FILESIZE_REQUIRE_BASE"] == "HEAD"
    assert calls[1][2]["NEWCODE_BASE"] == "HEAD"
    assert calls[1][2]["CHANGELOG_BASE"] == "HEAD"
    assert calls[-1][0][-2:] == ["--exempt", str(repo / "tools/ci_commit_exempt.txt")]


def test_all_families_after_scrub_failure(repo, monkeypatch, capsys):
    calls = fake_runner(monkeypatch, "scrub_check.py")
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [["cargo", "fmt"]])
    assert preflight.main(["--base", "HEAD"], repo) == 1
    assert len(calls) == 7
    assert re.findall(r'^(?:PASS|FAIL) (\w+) ', capsys.readouterr().out, re.M) == [
        'scrub', 'policy', 'rust', 'tests']


def test_retry_recovers(repo, monkeypatch, capsys):
    calls = []

    def run(*args):
        calls.append(args)
        return (1, "connection reset") if len(calls) == 1 else (0, "")

    monkeypatch.setattr(preflight, "run", run)
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    assert len(calls) == 6
    assert "PASS scrub " in capsys.readouterr().out


def test_rust_selection(repo, monkeypatch):
    crate = repo / "rust/one"
    crate.mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "one"\n')
    (crate / "lib.rs").write_text("// changed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "rust"], cwd=repo, check=True)
    monkeypatch.setattr(preflight.shutil, "which", lambda _: "/fake/cargo")
    commands = preflight.rust_commands(repo, "HEAD~1", "HEAD")
    assert commands[1] == ["cargo", "clippy", "-p", "one", "--all-targets", "--", "-D", "warnings"]
    assert commands[2] == ["cargo", "test", "-p", "one"]
    monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
    assert preflight.rust_commands(repo, "HEAD~1", "HEAD") == []


def test_words_and_tools_override(repo, monkeypatch):
    calls = fake_runner(monkeypatch)
    monkeypatch.setenv("PREFLIGHT_TOOLS_DIR", str(repo / "fake"))
    monkeypatch.setenv("PLANGRAPH_SCRUB_WORDS", str(repo / "private"))
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    assert calls[0][0][1] == str(repo / "fake/split/scrub_check.py")
    assert '--words' not in calls[0][0]
    assert calls[1][0][-2:] == ["--words", str(repo / "private")]


def test_fail_fast(repo, monkeypatch, capsys):
    calls = fake_runner(monkeypatch, 'scrub_check.py')
    assert preflight.main(['--base', 'HEAD', '--fail-fast'], repo) == 1
    assert len(calls) == 1
    assert 'SKIP tests ' in capsys.readouterr().out


@pytest.mark.parametrize('reverse', [False, True])
def test_exit_code_precedence(repo, monkeypatch, reverse):
    def run(command, cwd, env):
        name = Path(command[1]).name
        if name == 'scrub_check.py':
            return 1, 'rejected' if reverse else 'HTTP 503'
        if name == 'affected_tests.py':
            return 1, 'HTTP 503' if reverse else 'rejected'
        return 0, ''

    monkeypatch.setattr(preflight, 'run', run)
    assert preflight.main(['--base', 'HEAD'], repo) == 1


def private_scrub(repo, added):
    word = 'synthetic' + 'restrictedfixture'
    (repo / 'old.txt').write_text(word + '\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'baseline'],
                   cwd=repo, check=True)
    (repo / 'new.txt').write_text((word if added else 'clean') + '\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'change'],
                   cwd=repo, check=True)
    words = repo / 'words'
    words.write_text(word + '\n')
    result = subprocess.run([
        'python3', str(Path(preflight.__file__).with_name('preflight_private_scrub.py')),
        '--base', 'HEAD~1', '--head', 'HEAD', '--words', str(words)],
        cwd=repo, capture_output=True, text=True)
    assert word not in result.stdout + result.stderr
    return result


def test_private_word_untouched_file(repo):
    assert private_scrub(repo, False).returncode == 0


def test_private_word_added_line_redacted(repo):
    result = private_scrub(repo, True)
    assert result.returncode == 1
    assert 'new.txt:1: private-word' in result.stdout


@pytest.mark.parametrize('gate', ['held_commit', 'push_dry_run', 'push', None])
def test_writer_publish(repo, monkeypatch, capsys, gate):
    calls = []
    def run(command, cwd, env):
        calls.append(command)
        if 'symbolic-ref' in command:
            return 0, 'lane/test\n'
        if 'log' in command:
            return 0, 'aaa bbb\tMerge pull request from lane/test\0' if gate == 'held_commit' else ''
        if 'push' in command:
            return (1, '') if gate == ('push_dry_run' if '--dry-run' in command else 'push') else (0, '')
        return 0, ''
    monkeypatch.setattr(preflight, 'run', run)
    assert preflight.writer_push(repo, {}) == (1 if gate else 0)
    if gate:
        assert 'HOLD ' + gate in capsys.readouterr().out
    pushes = [c for c in calls if 'push' in c]
    assert len(pushes) == (0 if gate == 'held_commit' else 1 if gate == 'push_dry_run' else 2)


def test_prompt_empty():
    import asyncio
    from plangraph.spawner.runner import SpawnerRunner, IMPLEMENT_HEADER
    from plangraph.routing_errors import SpawnRefused
    for role in ('implement', 'review'):
        for prompt in (' \n\t', IMPLEMENT_HEADER + '\n\n'):
            with pytest.raises(SpawnRefused, match='prompt_empty'):
                asyncio.run(SpawnerRunner().spawn(prompt, model_or_combo='omniroute/cx-gpt-6.1-sol', role=role))
