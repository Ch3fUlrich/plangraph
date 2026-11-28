"""Hermetic preflight orchestration, without real policy tools or cargo."""
import importlib.util
import json
import tempfile
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import time

import pytest

SPEC = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).resolve().parents[1] / "tools/preflight.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def test_bounded_runner_imports_without_preflight(tmp_path):
    script = """
import subprocess
import sys

sys.path.insert(0, sys.argv[1])
from pgtest import run_test_process

assert "preflight" not in sys.modules
child = run_test_process(
    [sys.executable, "-c", "import sys; print(sys.stdin.read().strip())"],
    input="fixture", stdout=subprocess.PIPE, text=True, timeout=5, taskkill_timeout=5,
)
assert child.returncode == 0
assert child.stdout == "fixture\\n"
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, str(Path(preflight.__file__).parent)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("PREFLIGHT_TOOLS_DIR", raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-q",
                    "--allow-empty", "-m", "initial"], check=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CARGO_HOME", raising=False)
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
        if command[:2] == ["cargo", "build"]:
            # The policy step's pg-policy build: Cargo reports the executable it built.
            built = Path(tempfile.mkdtemp(prefix="fake-policy-")) / "pg-policy"
            built.write_text("")
            return 0, json.dumps({"reason": "compiler-artifact", "executable": str(built),
                                  "target": {"name": "pg-policy", "kind": ["bin"]}}) + "\n"
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


@pytest.mark.parametrize("real_failure", [False, True])
def test_clippy_failure_is_verified_outside_the_shared_target(repo, monkeypatch, capsys, real_failure):
    shared = repo / "shared-target"
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(shared))
    monkeypatch.setattr(preflight, "rust_target_env", lambda env, cleanup: env)
    monkeypatch.setattr(preflight, "running_disk_refusal", lambda root: None)
    monkeypatch.setattr(preflight, "require_protoc", lambda *args: None)
    command = ["cargo", "clippy", "-p", "fixture", "--all-targets"]
    monkeypatch.setattr(preflight, "rust_commands", lambda *args: [command])
    targets = []

    def run(args, cwd, env):
        if args != command:
            return 0, ""
        target = Path(env["CARGO_TARGET_DIR"])
        targets.append(target)
        if target == shared:
            return 1, "error[E0432]: unresolved import from mixed build artifacts\n"
        assert target.is_dir() and not list(target.iterdir())
        return (1, "error[E0425]: cannot find value in this scope\n") if real_failure else (0, "")

    monkeypatch.setattr(preflight, "run", run)
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == int(real_failure)
    assert len(targets) == 2 and targets[0] == shared and targets[1] != shared
    assert not targets[1].exists()
    output = capsys.readouterr().out
    assert f"{'FAIL' if real_failure else 'PASS'} rust " in output
    assert str(targets[1]) in output
    assert ("error[E0425]" in output) == real_failure
    assert "error[E0432]" not in output


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
    assert calls[-1][0][-4:] == ["--base", "HEAD", "--head", "HEAD"]


def test_commit_policy_catches_removals_before_the_affected_range(repo, monkeypatch, capsys):
    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=repo, text=True).strip()

    test_file = repo / "test_example.py"
    test_file.write_text("def test_old(): pass\n")
    git("add", str(test_file))
    git("commit", "-qm", "Add fixture test")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("rm", str(test_file))
    git("commit", "-qm", "Delete fixture test\n\nWriter: fixture")
    removed_at = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "Newer change\n\nWriter: fixture")
    gate = Path(preflight.__file__).with_name("ci_commit_gate.py")
    expected = subprocess.run([sys.executable, str(gate), "check", "--base", "origin/main",
                               "--head", "HEAD"], cwd=repo, capture_output=True, text=True)
    assert expected.returncode == 1
    assert expected.stdout.strip() == f"{removed_at}: test removal without Test-Removal trailer"

    original = preflight.run
    def run(command, cwd, env):
        name = Path(command[1]).name
        return original(command, cwd, env) if name == gate.name else (0, "")

    monkeypatch.setattr(preflight, "run", run)
    assert preflight.main(["--base", removed_at, "--fast"], repo) == 1
    assert expected.stdout in capsys.readouterr().out


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


def test_policy_reports_every_failing_check(repo, monkeypatch, capsys):
    def run(command, cwd, env):
        name = Path(command[1]).name
        bad = {"check_file_size.py": "FAIL a.py: too long\nmore", "check_changelog_docs.py": "FAIL no changelog entry"}
        return (1, bad[name] + "\n") if name in bad else (0, "")

    monkeypatch.setattr(preflight, "run", run)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [["cargo", "fmt"]])
    assert preflight.main(["--base", "HEAD"], repo) == 1
    out = capsys.readouterr().out
    assert "policy: 2 check(s) failed:" in out
    assert "  check_file_size.py: FAIL a.py: too long" in out
    assert "  check_changelog_docs.py: FAIL no changelog entry" in out
    assert re.search(r"^FAIL policy ", out, re.M)


def test_affected_timeout_names_step_bound_and_count(monkeypatch, repo):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b'{"tests": ["a", "b", "c"]}\n')

    monkeypatch.setattr(preflight, "run_test_process", timeout)
    monkeypatch.setattr(preflight, "host_load", lambda: (0.0, 8))  # a busy host adds a reason line
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


@pytest.mark.parametrize("pin_only", [False, True])
def test_rust_selection(repo, monkeypatch, pin_only):
    crate = repo / "rust/one"
    crate.mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "one"\n')
    (crate / "lib.rs").write_text("// changed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "rust"], cwd=repo, check=True)
    if pin_only:
        (repo / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.100.0"\n')
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "pin"], cwd=repo, check=True)
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
    reason = ("CARGO FAIL: cargo is required for Rust checks" if rust_changed
              else "rust: SKIP: no rust changes")
    assert [line for line in output.splitlines() if line.startswith(("rust:", "CARGO FAIL:"))] == [reason]
    assert re.search(rf"^{'FAIL' if rust_changed else 'SKIP'} rust ", output, re.M)


def test_words_and_tools_override(repo, monkeypatch):
    calls = fake_runner(monkeypatch)
    monkeypatch.setenv("PREFLIGHT_TOOLS_DIR", str(repo / "fake"))
    monkeypatch.setenv("PLANGRAPH_SCRUB_WORDS", str(repo / "private"))
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    assert calls[0][0][1] == str(repo / "fake/split/scrub_check.py")
    assert calls[0][0][-2:] == ["--words", str(repo / "private")]
    assert all('preflight_private_scrub.py' not in command for command, *_ in calls)


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
        sys.executable, str(Path(preflight.__file__).with_name('preflight_private_scrub.py')),
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


def test_affected_command_carries_host_sized_workers(repo, monkeypatch):
    """LE-PREPUSH-TESTS-ON-BUSIEST-HOST (3): the submitted affected-tests command carries the derived count."""
    seen = []

    def run(command, cwd, env):
        if Path(command[1]).name == "affected_tests.py":
            seen.append(command)
        return 0, ""

    monkeypatch.setattr(preflight, "run", run)
    monkeypatch.setattr(preflight, "available_memory_mb", lambda: 20 * 1024)
    monkeypatch.setattr(preflight.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(preflight.importlib.util, "find_spec",
                        lambda name, *a: object() if name == "xdist" else importlib.util.find_spec(name, *a))
    monkeypatch.delenv("PREFLIGHT_TEST_WORKERS", raising=False)
    preflight.main(["--base", "HEAD", "--head", "HEAD"], repo)
    assert seen and seen[0][seen[0].index("--workers") + 1] == "10"


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
    assert "--files-only" in seen[0]


def test_timeout_is_failure(monkeypatch, repo):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == preflight.CHECK_TIMEOUT
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(preflight.subprocess, "run", timeout)
    assert preflight.run(["hung"], repo, {}) == (1, "check exceeded 60 second timeout\n")


def _rust_main(repo, monkeypatch, fail=False):
    seen = []

    def run(command, cwd, env):
        if command[0] == "cargo":
            seen.append(env)
            return (1, "boom\n") if fail else (0, "")
        return 0, ""

    monkeypatch.setattr(preflight, "run", run)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [["cargo", "fmt", "--check"]])
    monkeypatch.setattr(preflight, "cargo_lease", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(preflight, "running_disk_refusal", lambda root: None)
    monkeypatch.setenv("PLANGRAPH_PREFLIGHT_RUN_DIR", str(repo / "runs"))
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(repo / "shared"))
    code = preflight.main(["--base", "HEAD", "--fast"], repo)
    return code, seen


def _fake_sccache(repo, monkeypatch):
    bindir = repo / "bin"
    bindir.mkdir(exist_ok=True)
    tool = bindir / "sccache"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("RUSTC_WRAPPER", raising=False)
    monkeypatch.delenv("SCCACHE_CACHE_SIZE", raising=False)
    monkeypatch.delenv("PLANGRAPH_SCCACHE_CACHE_SIZE", raising=False)
    return tool


def test_private_target_with_sccache_and_removed(repo, monkeypatch):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    tool = _fake_sccache(repo, monkeypatch)
    code, seen = _rust_main(repo, monkeypatch)
    target = Path(seen[0]["CARGO_TARGET_DIR"])
    assert code == 0 and target.parent == repo / "runs" and target != repo / "shared"
    assert not target.exists() and seen[0]["RUSTC_WRAPPER"] == str(tool)


def test_shared_target_without_sccache(repo, monkeypatch, capsys):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    monkeypatch.setenv("PATH", str(repo / "nobin"))
    monkeypatch.delenv("RUSTC_WRAPPER", raising=False)
    code, seen = _rust_main(repo, monkeypatch)
    assert code == 0 and seen[0]["CARGO_TARGET_DIR"] == str(repo / "shared")
    assert "RUSTC_WRAPPER" not in seen[0] and not (repo / "runs").exists()
    assert "no sccache" in capsys.readouterr().out


def test_private_target_removed_on_failure(repo, monkeypatch):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    _fake_sccache(repo, monkeypatch)
    code, seen = _rust_main(repo, monkeypatch, fail=True)
    assert code == 1 and not Path(seen[0]["CARGO_TARGET_DIR"]).exists()


def test_private_target_removed_on_system_exit(repo, monkeypatch):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    _fake_sccache(repo, monkeypatch)
    seen = []

    def run(command, cwd, env):
        if command[0] == "cargo":
            seen.append(env["CARGO_TARGET_DIR"])
            assert Path(env["CARGO_TARGET_DIR"]).is_dir()
            raise SystemExit(2)
        return 0, ""

    monkeypatch.setattr(preflight, "run", run)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [["cargo", "fmt", "--check"]])
    monkeypatch.setattr(preflight, "cargo_lease", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(preflight, "running_disk_refusal", lambda root: None)
    monkeypatch.setenv("PLANGRAPH_PREFLIGHT_RUN_DIR", str(repo / "runs"))
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(repo / "shared"))
    with pytest.raises(SystemExit):
        preflight.main(["--base", "HEAD", "--fast"], repo)
    assert len(seen) == 1 and str(repo / "runs") in seen[0] and not Path(seen[0]).exists()


def test_stale_private_targets_pruned(repo, monkeypatch):
    runs = repo / "runs"
    old, fresh, other = runs / "preflight-cargo-old", runs / "preflight-cargo-new", runs / "keep"
    for d in (old, fresh, other):
        (d / "x").mkdir(parents=True)
    past = time.time() - preflight.STALE_TARGET_SECONDS - 60
    for d in (old, other):
        os.utime(d, (past, past))
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    _fake_sccache(repo, monkeypatch)
    _rust_main(repo, monkeypatch)
    assert not old.exists() and fresh.exists() and other.exists()


def test_shared_target_when_disk_low(repo, monkeypatch, capsys):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 1)
    _fake_sccache(repo, monkeypatch)
    code, seen = _rust_main(repo, monkeypatch)
    assert code == 0 and seen[0]["CARGO_TARGET_DIR"] == str(repo / "shared")
    assert "rust: shared CARGO_TARGET_DIR (free 1.0 GB < 20 GB)" in capsys.readouterr().out
    assert not (repo / "runs").exists() or not list((repo / "runs").iterdir())


def test_invalid_min_free_says_so(repo, monkeypatch, capsys):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    _fake_sccache(repo, monkeypatch)
    monkeypatch.setenv("PLANGRAPH_PREFLIGHT_MIN_FREE_GB", "lots")
    code, seen = _rust_main(repo, monkeypatch)
    assert code == 0 and seen[0]["CARGO_TARGET_DIR"] == str(repo / "shared")
    assert "invalid PLANGRAPH_PREFLIGHT_MIN_FREE_GB='lots'" in capsys.readouterr().out


def test_disk_free_none_falls_back(repo, monkeypatch):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: None)
    _fake_sccache(repo, monkeypatch)
    code, seen = _rust_main(repo, monkeypatch)
    assert code == 0 and seen[0]["CARGO_TARGET_DIR"] == str(repo / "shared")


def test_min_free_threshold_from_env(repo, monkeypatch):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 5)
    _fake_sccache(repo, monkeypatch)
    monkeypatch.setenv("PLANGRAPH_PREFLIGHT_MIN_FREE_GB", "3")
    _, seen = _rust_main(repo, monkeypatch)
    assert seen[0]["CARGO_TARGET_DIR"] != str(repo / "shared")


def test_sccache_size_from_plangraph_var_wins(repo, monkeypatch):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    _fake_sccache(repo, monkeypatch)
    monkeypatch.setenv("SCCACHE_CACHE_SIZE", "1G")
    monkeypatch.setenv("PLANGRAPH_SCCACHE_CACHE_SIZE", "7G")
    _, seen = _rust_main(repo, monkeypatch)
    assert seen[0]["SCCACHE_CACHE_SIZE"] == "7G"


def test_inherited_wrapper_kept_with_sccache(repo, monkeypatch):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    _fake_sccache(repo, monkeypatch)
    monkeypatch.setenv("RUSTC_WRAPPER", "/opt/wrap")
    _, seen = _rust_main(repo, monkeypatch)
    assert seen[0]["RUSTC_WRAPPER"] == "/opt/wrap"
    assert seen[0].get("CARGO_TARGET_DIR") == str(repo / "shared")


def test_inherited_sccache_wrapper_uses_private_target(repo, monkeypatch):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    tool = _fake_sccache(repo, monkeypatch)
    monkeypatch.setenv("RUSTC_WRAPPER", str(tool))
    _, seen = _rust_main(repo, monkeypatch)
    assert str(repo / "runs") in seen[0]["CARGO_TARGET_DIR"]


@pytest.mark.parametrize("raw", ["nan", "inf", "-1", "abc"])
def test_invalid_min_free_gb_uses_shared_target(repo, monkeypatch, capsys, raw):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    _fake_sccache(repo, monkeypatch)
    monkeypatch.setenv("PLANGRAPH_PREFLIGHT_MIN_FREE_GB", raw)
    _, seen = _rust_main(repo, monkeypatch)
    assert seen[0].get("CARGO_TARGET_DIR") == str(repo / "shared")
    assert "invalid PLANGRAPH_PREFLIGHT_MIN_FREE_GB" in capsys.readouterr().out


def test_unknown_free_disk_message(repo, monkeypatch, capsys):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: None)
    _fake_sccache(repo, monkeypatch)
    _, seen = _rust_main(repo, monkeypatch)
    assert seen[0].get("CARGO_TARGET_DIR") == str(repo / "shared")
    assert "free disk unknown" in capsys.readouterr().out


def test_inherited_wrapper_kept_without_sccache(repo, monkeypatch):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    monkeypatch.setenv("PATH", str(repo / "nobin"))
    monkeypatch.setenv("RUSTC_WRAPPER", "/opt/wrap")
    _, seen = _rust_main(repo, monkeypatch)
    assert seen[0]["RUSTC_WRAPPER"] == "/opt/wrap"


def test_missing_named_sccache_is_ignored(repo, monkeypatch, capsys):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    monkeypatch.setenv("PATH", str(repo / "nobin"))
    monkeypatch.delenv("RUSTC_WRAPPER", raising=False)
    monkeypatch.setenv("PLANGRAPH_SCCACHE", str(repo / "typo-sccache"))
    _, seen = _rust_main(repo, monkeypatch)
    assert "RUSTC_WRAPPER" not in seen[0]
    assert "not found, no sccache" in capsys.readouterr().out


def test_disk_probe_uses_existing_ancestor(repo, monkeypatch):
    probed = []

    def free(path):
        assert Path(path).exists()
        probed.append(Path(path))
        return 100

    monkeypatch.setattr(preflight, "disk_free_gb", free)
    _fake_sccache(repo, monkeypatch)
    _, seen = _rust_main(repo, monkeypatch)
    assert probed == [repo] and Path(seen[0]["CARGO_TARGET_DIR"]).parent == repo / "runs"


def test_private_target_failure_falls_back_to_shared(repo, monkeypatch, capsys):
    monkeypatch.setattr(preflight, "disk_free_gb", lambda path: 100)
    _fake_sccache(repo, monkeypatch)

    def broken(**kwargs):
        raise OSError("mkdtemp failed")

    monkeypatch.setattr(preflight.tempfile, "mkdtemp", broken)
    code, seen = _rust_main(repo, monkeypatch)
    assert code == 0 and seen[0]["CARGO_TARGET_DIR"] == str(repo / "shared")
    assert "rust: shared CARGO_TARGET_DIR (mkdtemp failed)" in capsys.readouterr().out


def test_cargo_lease_is_shared_between_runs(tmp_path, monkeypatch):
    import threading
    from plangraph.spawner.host_ready import cargo_lease
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(tmp_path / "shared"))
    first = cargo_lease()
    got = []
    worker = threading.Thread(target=lambda: got.append(cargo_lease()), daemon=True)
    try:
        worker.start()
        worker.join(5)
        assert not worker.is_alive() and got, "second shared lease blocked"
        with pytest.raises(BlockingIOError):
            cargo_lease(exclusive=True)
    finally:
        first.close()
        for lease in got:
            lease.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux disk hygiene")
@pytest.mark.parametrize(("reply", "message"), [
    ((0, "{}\n"), "cargo post-run trim result: {}"),
    ((2, "Usage: pg-spawnd host <work-disk|wsl-cache-guard>\n"), None),
    ((2, "process inventory unavailable\n"), "cargo post-run trim skipped: process inventory unavailable"),
])
def test_post_run_trim_executes_after_cargo_lease_is_released(repo, monkeypatch, capsys, reply, message):
    from plangraph.spawner.host_ready import cargo_lease

    target = repo.parent / (repo.name + "-post-run-cache")
    target.mkdir()
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    monkeypatch.setattr(preflight, "trim_shared_target", lambda root: None)
    monkeypatch.setattr(preflight, "rust_commands", lambda *args: [["cargo", "fmt", "--check"]])
    monkeypatch.setattr(preflight, "running_disk_refusal", lambda root: None)
    calls = fake_runner(monkeypatch)
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kw:
                        "/fixture/pg-spawnd" if name == "pg-spawnd" else real_which(name, *args, **kw))
    real_run = preflight.run

    def run(command, cwd, env):
        if command[0] == "/fixture/pg-spawnd":
            assert calls[-1][0][:2] == ["cargo", "fmt"]
            with cargo_lease(exclusive=True):
                calls.append((command, cwd, env))
            return reply
        return real_run(command, cwd, env)

    monkeypatch.setattr(preflight, "run", run)
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    assert calls[-1][0] == ["/fixture/pg-spawnd", "host", "post-run-cargo"]
    output = capsys.readouterr().out
    if message is None:
        assert "cargo post-run trim" not in output
    else:
        assert message in output
