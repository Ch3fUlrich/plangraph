"""Tests for pre-push refusal on fixture identity in local repo config."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from git_identity import identity_args

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "tools" / "hooks" / "pre-push"


def _set_local_identity(repo: Path, name: str | None = None, email: str | None = None) -> None:
    if name is not None:
        subprocess.run(["git", "-C", str(repo), "config", "--local", "user.name", name], check=True)  # allow-local-identity: tests the pre-push refusal
    if email is not None:
        subprocess.run(["git", "-C", str(repo), "config", "--local", "user.email", email], check=True)  # allow-local-identity: tests the pre-push refusal


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "clean.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(r), *identity_args(), "commit", "-qm", "init"], check=True)
    sha = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    return r, sha


def _run_hook(repo: Path, head_sha: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    env.pop("PLANGRAPH_SCRUB_WORDS", None)
    env.pop("PLANGRAPH_SCRUB_RULES", None)
    stdin = f"refs/heads/main {head_sha} refs/heads/main {'0' * 40}\n"
    return subprocess.run(
        [sys.executable, str(HOOK), "origin", "https://example.invalid/r.git"],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.mark.parametrize("name,email", [
    ("test", "test@example.invalid"),
    ("test", "test@example.com"),
    ("test", "test@example.org"),
    ("risk-test", "user@corp.example"),
    ("Tester", "some-risk-test@corp.example"),
])
def test_prepush_refuses_fixture_identity(tmp_path: Path, name: str, email: str) -> None:
    repo, head = _make_repo(tmp_path)
    _set_local_identity(repo, name=name, email=email)
    res = _run_hook(repo, head)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "fixture-identity" in res.stderr
    lines = [line for line in res.stderr.splitlines() if "fixture-identity" in line]
    assert len(lines) == 1


def test_prepush_passes_real_looking_identity(tmp_path: Path) -> None:
    repo, head = _make_repo(tmp_path)
    _set_local_identity(repo, name="Real Engineer", email="engineer@corp.example")
    res = _run_hook(repo, head)
    assert res.returncode == 0, res.stdout + res.stderr


def test_prepush_passes_unset_identity(tmp_path: Path) -> None:
    repo, head = _make_repo(tmp_path)
    res = _run_hook(repo, head)
    assert res.returncode == 0, res.stdout + res.stderr


def test_prepush_fails_closed_on_unreadable_local_config(tmp_path: Path) -> None:
    repo, head = _make_repo(tmp_path)
    (repo / ".git" / "config").write_text("[user\n\tname = x\n", encoding="utf-8")
    res = _run_hook(repo, head)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "identity-check" in res.stderr
    assert "fixture-identity" not in res.stderr


def _real_python3() -> bool:
    try:
        return subprocess.run(["python3", "-c", "pass"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_python3 = pytest.mark.skipif(
    not _real_python3(), reason="the hook's shebang needs a working python3 on PATH")


def _real_push(repo: Path, remote: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    env.pop("PLANGRAPH_SCRUB_WORDS", None)
    env.pop("PLANGRAPH_SCRUB_RULES", None)
    return subprocess.run(
        ["git", "-C", str(repo), "-c", f"core.hooksPath={HOOK.parent.as_posix()}",
         "push", str(remote), "main"],
        capture_output=True, text=True, env=env, timeout=120)


@needs_python3
@pytest.mark.parametrize("fixture_identity,pushed", [(True, False), (False, True)])
def test_real_git_push_runs_the_hook(tmp_path: Path, fixture_identity: bool, pushed: bool) -> None:
    repo, _ = _make_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    if fixture_identity:
        _set_local_identity(repo, name="test", email="test@example.invalid")
    res = _real_push(repo, remote)
    branches = subprocess.run(["git", "-C", str(remote), "branch", "--list", "main"],
                              capture_output=True, text=True, check=True).stdout.strip()
    assert bool(branches) is pushed, res.stdout + res.stderr
    assert (res.returncode == 0) is pushed, res.stdout + res.stderr
    if not pushed:
        assert "fixture-identity" in res.stderr
