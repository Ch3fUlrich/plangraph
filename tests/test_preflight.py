"""Hermetic preflight orchestration, without real policy tools or cargo."""
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

SPEC = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).resolve().parents[1] / "tools/preflight.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("PREFLIGHT_TOOLS_DIR", raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-q",
                    "--allow-empty", "-m", "initial"], check=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PLANGRAPH_SCRUB_WORDS", raising=False)
    return tmp_path


def test_ambient_tools_dir_ignored(tmp_path, monkeypatch, request):
    monkeypatch.setenv("PREFLIGHT_TOOLS_DIR", "/nonexistent/ambient-preflight-tools")
    isolated = request.getfixturevalue("repo")
    calls = fake_runner(monkeypatch)
    assert preflight.main(["--base", "HEAD", "--fast"], isolated) == 0
    assert Path(calls[0][0][1]) == Path(preflight.__file__).parent / "split/scrub_check.py"


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
    output = capsys.readouterr().out
    assert output.count("rerun once") == 1
    assert "INFRA scrub " in output


def test_fast_summary_and_ci_env(repo, monkeypatch, capsys):
    calls = fake_runner(monkeypatch)
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if re.match(r"^(PASS|SKIP) ", line)]
    assert [line.split()[:2] for line in lines] == [
        ["SKIP", "scrub"], ["PASS", "policy"], ["SKIP", "rust"], ["SKIP", "tests"]]
    assert all(re.fullmatch(r"(?:PASS|SKIP) \w+ \d+\.\d{3}", line) for line in lines)
    assert len(calls) == 5
    assert calls[1][2]["FILESIZE_REQUIRE_BASE"] == "HEAD"
    assert calls[1][2]["NEWCODE_BASE"] == "HEAD"
    assert calls[1][2]["CHANGELOG_BASE"] == "HEAD"
    assert calls[-1][0][-2:] == ["--exempt", str(Path(preflight.__file__).with_name("ci_commit_exempt.txt"))]


@pytest.mark.parametrize("base,expected", [("HEAD~1", 0), ("HEAD", 1)])
def test_policy_base_stays_enforced_without_leaking_into_test_repos(
        repo, monkeypatch, capsys, base, expected):
    subprocess.run(["git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-q",
                    "--allow-empty", "-m", "change"], cwd=repo, check=True)
    nested = repo / "nested-checkout"
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-q",
                    "--allow-empty", "-m", "fixture"], cwd=nested, check=True)
    controls = ("FILESIZE_REQUIRE_BASE", "NEWCODE_BASE", "CHANGELOG_BASE")
    for key in controls:
        monkeypatch.setenv(key, "ambient-policy-only-ref")
    original = preflight.run
    nested_results, seen = [], []
    policy_tools = {"check_file_size.py", "check_new_code_lang.py",
                    "check_changelog_docs.py", "ci_commit_gate.py"}
    checker = Path(preflight.__file__).with_name("check_file_size.py")

    def run(command, cwd, env):
        name = Path(command[1]).name
        seen.append((name, dict(env)))
        if name in policy_tools:
            assert all(env[key] == base for key in controls)
        if name == "check_file_size.py":
            return original(command, cwd, env)
        if name == "affected_tests.py":
            result = original([sys.executable, str(checker)], nested, env)
            nested_results.append(result)
            return result
        return 0, ""

    monkeypatch.setattr(preflight, "run", run)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [["cargo", "fmt", "--check"]])
    assert preflight.main(["--base", base, "--head", "HEAD"], repo) == expected
    assert nested_results and nested_results[0][0] == 0, nested_results
    assert all(not any(key in env for key in controls)
               for name, env in seen if name not in policy_tools)
    output = capsys.readouterr().out
    assert f"{'FAIL' if expected else 'PASS'} policy " in output
    assert "PASS tests " in output
    if expected:
        assert "FILESIZE_REQUIRE_BASE=HEAD is HEAD itself" in output


def test_all_families_after_scrub_failure(repo, monkeypatch, capsys):
    calls = fake_runner(monkeypatch, "scrub_check.py")
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [["cargo", "fmt"]])
    assert preflight.main(["--base", "HEAD"], repo) == 1
    assert len(calls) == 6 and not any(Path(c[0][1]).name == "affected_tests.py" for c in calls)
    out = capsys.readouterr().out
    assert re.findall(r'^(?:PASS|FAIL|SKIP) (\w+) ', out, re.M) == ['scrub', 'policy', 'rust', 'tests']
    assert re.search(r"^SKIP tests ", out, re.M)  # scrub refused the push: no point running tests


def test_affected_timeout_names_step_bound_and_count(monkeypatch, repo):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b'{"tests": ["a", "b", "c"]}\n')

    monkeypatch.setattr(preflight.subprocess, "run", timeout)
    code, text = preflight.run(["python3", "tools/affected_tests.py"], repo, {})
    assert code == 1
    assert text == (f"tests: affected-tests step exceeded its {preflight.TEST_TIMEOUT} second bound "
                    "(3 selected test paths)\n")


def test_retry_recovers(repo, monkeypatch, capsys):
    calls = []

    def run(*args):
        calls.append(args)
        return (1, "connection reset") if len(calls) == 1 else (0, "")

    monkeypatch.setattr(preflight, "run", run)
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 1
    assert len(calls) == 5
    assert "FAIL scrub " in capsys.readouterr().out


def test_http_retry_recovers(repo, monkeypatch, capsys):
    calls = []

    def run(*args):
        calls.append(args)
        return (1, "HTTP/1.1 503 unavailable\n") if len(calls) == 1 else (0, "")

    monkeypatch.setattr(preflight, "run", run)
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    assert len(calls) == 6
    assert "rerun once" in capsys.readouterr().out


def test_default_root_is_script_relative(repo, monkeypatch):
    calls = fake_runner(monkeypatch)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [])
    assert preflight.main(["--fast"]) == 0
    assert all(cwd == Path(preflight.__file__).resolve().parents[1]
               for _, cwd, _ in calls)


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


@pytest.mark.parametrize("rust_changed", [False, True])
def test_cargo_absent(repo, monkeypatch, capsys, rust_changed):
    if rust_changed:
        (repo / "rust").mkdir()
        (repo / "rust/lib.rs").write_text("// changed\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "rust"],
                       cwd=repo, check=True)
    bin_dir = repo / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(bin_dir))
    assert shutil.which("cargo") is None
    fake_runner(monkeypatch)
    base = "HEAD~1" if rust_changed else "HEAD"
    assert preflight.main(["--base", base, "--fast"], repo) == int(rust_changed)
    output = capsys.readouterr().out
    reason = ("rust: FAIL: missing required tool: cargo" if rust_changed
              else "rust: SKIP: no rust changes")
    assert [line for line in output.splitlines() if line.startswith("rust:")] == [reason]
    assert re.search(rf"^{'FAIL' if rust_changed else 'SKIP'} rust ", output, re.M)


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


@pytest.mark.parametrize("text", ["500 file-size limit", "download test failed", "timed out assertion"])
def test_check_output_is_not_infra(repo, monkeypatch, capsys, text):
    calls = fake_runner(monkeypatch, "scrub_check.py", text)
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 1
    assert len(calls) == 5
    assert "INFRA" not in capsys.readouterr().out


def test_real_affected_argv(repo, monkeypatch):
    original = preflight.run
    seen = []

    def run(command, cwd, env):
        if Path(command[1]).name != "affected_tests.py":
            return 0, ""
        seen.append(command)
        # Exercise the generated flags with the actual parser, in an empty repo.
        command[1] = str(Path(preflight.__file__).with_name("affected_tests.py"))
        return original([*command, "--root", str(repo)], cwd, env)

    monkeypatch.setattr(preflight, "run", run)
    assert preflight.main(["--base", "HEAD", "--head", "HEAD"], repo) == 0
    assert seen and seen[0][seen[0].index("--head") + 1] == "HEAD"


def test_timeout_is_failure(monkeypatch, repo):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == preflight.CHECK_TIMEOUT
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(preflight.subprocess, "run", timeout)
    assert preflight.run(["hung"], repo, {}) == (1, "check exceeded 60 second timeout\n")


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
    monkeypatch.setattr(preflight, "run", run)
    monkeypatch.setattr(preflight, "test_environment", lambda root, env: env)
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
