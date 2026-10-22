from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "tools" / "ci_commit_gate.py"
EXEMPT_PATH = REPO_ROOT / "tools" / "ci_commit_exempt.txt"

spec = importlib.util.spec_from_file_location("ci_commit_gate", MODULE_PATH)
assert spec is not None and spec.loader is not None
ci_commit_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_commit_gate)

check = ci_commit_gate.check
main = ci_commit_gate.main
load_exempt = ci_commit_gate.load_exempt
ExemptError = ci_commit_gate.ExemptError

BASE_MSG = "chore: seed\n\nWriter: mimo-v2.6-flash run-base\n"
OK_MSG = "feat: change\n\nWriter: mimo-v2.6-flash run-1\n"


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "plangraph-ci",
            "GIT_AUTHOR_EMAIL": "ci@example.invalid",
            "GIT_COMMITTER_NAME": "plangraph-ci",
            "GIT_COMMITTER_EMAIL": "ci@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_git_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _commit(
    repo: Path,
    message: str,
    files: dict[str, str] | None = None,
    remove: tuple[str, ...] = (),
) -> str:
    for name, content in (files or {}).items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for name in remove:
        (repo / name).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _head(repo)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _commit(path, BASE_MSG)
    return path


def test_missing_writer_trailer_fails(repo: Path):
    base = _head(repo)
    sha = _commit(repo, "feat: no trailer at all")
    assert check(base, sha, cwd=repo) == [f"{sha}: missing Writer trailer"]


def test_valid_writer_passes(repo: Path):
    base = _head(repo)
    sha = _commit(repo, OK_MSG)
    assert check(base, sha, cwd=repo) == []


@pytest.mark.parametrize(
    "writer",
    [
        "claude",
        "Claude Sonnet 4.5 run-1",
        "anthropic/opus run-1",
        "SONNET run-2",
        "Haiku run-3",
        "fable run-4",
    ],
)
def test_claude_family_writers_fail(repo: Path, writer: str):
    base = _head(repo)
    sha = _commit(repo, f"feat: served model\n\nWriter: {writer}\n")
    assert check(base, sha, cwd=repo) == [f"{sha}: claude writer"]


def test_unrelated_writer_model_passes(repo: Path):
    base = _head(repo)
    sha = _commit(repo, "feat: served model\n\nWriter: mimo-v2.6-flash run-9\n")
    assert check(base, sha, cwd=repo) == []


def test_last_writer_line_wins(repo: Path):
    base = _head(repo)
    message = (
        "feat: two writers\n\n"
        "Writer: claude run-old\n\n"
        "Writer: mimo-v2.6-flash run-1\n"
    )
    sha = _commit(repo, message)
    assert check(base, sha, cwd=repo) == []


def test_merge_commit_without_trailer_is_ignored(repo: Path):
    base = _head(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "feat: side\n\nWriter: mimo-v2.6-flash run-side\n")
    _git(repo, "checkout", "-q", "-")
    _commit(repo, "feat: main\n\nWriter: mimo-v2.6-flash run-main\n")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'side' (no trailer)", "side")
    merge_sha = _head(repo)

    message = _git(repo, "log", "-1", "--format=%B", merge_sha)
    assert "Writer:" not in message
    assert check(base, merge_sha, cwd=repo) == []


def test_exempt_sha_skips_all_rules(repo: Path, tmp_path: Path):
    base = _head(repo)
    sha = _commit(repo, "feat: no trailer at all")
    exempt = tmp_path / "exempt.txt"
    exempt.write_text(f"# note\n\n{sha}\n", encoding="utf-8")

    assert check(base, sha, exempt=exempt, cwd=repo) == []
    # Without the exemption the very same commit is a violation.
    assert check(base, sha, cwd=repo) == [f"{sha}: missing Writer trailer"]


def test_deleted_test_file_without_trailer_fails(repo: Path):
    base = _head(repo)
    _commit(
        repo,
        "test: add coverage\n\nWriter: mimo-v2.6-flash run-1\n",
        files={"tests/test_x.py": "def test_alpha():\n    pass\n"},
    )
    sha = _commit(
        repo,
        "chore: drop coverage\n\nWriter: mimo-v2.6-flash run-2\n",
        remove=("tests/test_x.py",),
    )
    assert check(base, sha, cwd=repo) == [
        f"{sha}: test removal without Test-Removal trailer"
    ]


def test_deleted_test_file_with_trailer_passes(repo: Path):
    base = _head(repo)
    _commit(
        repo,
        "test: add coverage\n\nWriter: mimo-v2.6-flash run-1\n",
        files={"tests/test_x.py": "def test_alpha():\n    pass\n"},
    )
    sha = _commit(
        repo,
        "chore: drop coverage\n\nWriter: mimo-v2.6-flash run-2\n\n"
        "Test-Removal: D-2047\n",
        remove=("tests/test_x.py",),
    )
    assert check(base, sha, cwd=repo) == []


def test_removed_test_function_without_trailer_fails(repo: Path):
    base = _head(repo)
    _commit(
        repo,
        "test: seed\n\nWriter: mimo-v2.6-flash run-1\n",
        files={"tests/test_y.py": "def test_beta():\n    pass\n"},
    )
    sha = _commit(
        repo,
        "refactor: drop the test body\n\nWriter: mimo-v2.6-flash run-2\n",
        files={"tests/test_y.py": "def helper():\n    pass\n"},
    )
    assert check(base, sha, cwd=repo) == [
        f"{sha}: test removal without Test-Removal trailer"
    ]


def test_removed_async_test_function_with_trailer_passes(repo: Path):
    base = _head(repo)
    _commit(
        repo,
        "test: seed\n\nWriter: mimo-v2.6-flash run-1\n",
        files={"tests/test_y.py": "async def test_beta():\n    pass\n"},
    )
    sha = _commit(
        repo,
        "refactor: drop the test body\n\nWriter: mimo-v2.6-flash run-2\n\n"
        "Test-Removal: D-94\n",
        files={"tests/test_y.py": "async def helper():\n    pass\n"},
    )
    assert check(base, sha, cwd=repo) == []


def test_test_removal_trailer_without_removed_test_is_not_a_violation(repo: Path):
    base = _head(repo)
    sha = _commit(
        repo,
        "feat: no test touched\n\nWriter: mimo-v2.6-flash run-1\n\n"
        "Test-Removal: D-2047\n",
    )
    assert check(base, sha, cwd=repo) == []


def test_co_authored_by_claude_passes(repo: Path):
    base = _head(repo)
    message = (
        "feat: fine\n\n"
        "Writer: mimo-v2.6-flash run-1\n\n"
        "Co-Authored-By: Claude Assistant\n"
    )
    sha = _commit(repo, message)
    assert check(base, sha, cwd=repo) == []


def test_bad_exempt_line_exits_2(repo: Path, tmp_path: Path, monkeypatch, capsys):
    base = _head(repo)
    sha = _commit(repo, OK_MSG)
    exempt = tmp_path / "bad_exempt.txt"
    exempt.write_text("# comment\n\nnot-a-sha\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    ret = main(["check", "--base", base, "--head", sha, "--exempt", str(exempt)])
    captured = capsys.readouterr()
    assert ret == 2
    assert captured.out == ""
    assert "exempt" in captured.err


def test_load_exempt_rejects_bad_line(tmp_path: Path):
    exempt = tmp_path / "exempt.txt"
    exempt.write_text("#94\n24e34a9ab52790e0e7a3de32e0d32915cddf6ffb\ngarbage\n",
                      encoding="utf-8")
    with pytest.raises(ExemptError):
        load_exempt(exempt)


def test_cli_check_ok_prints_nothing(repo: Path, monkeypatch, capsys):
    base = _head(repo)
    sha = _commit(repo, OK_MSG)
    monkeypatch.chdir(repo)
    # No --exempt: the default file next to the script is used and loads cleanly.
    assert main(["check", "--base", base, "--head", sha]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_cli_check_violation_exits_1_with_one_line(repo: Path, monkeypatch, capsys):
    base = _head(repo)
    sha = _commit(repo, "feat: no trailer at all")
    monkeypatch.chdir(repo)
    ret = main(["check", "--base", base, "--head", sha, "--exempt", str(EXEMPT_PATH)])
    captured = capsys.readouterr()
    assert ret == 1
    assert captured.out.strip() == f"{sha}: missing Writer trailer"


def test_real_exempt_file_is_frozen_and_well_formed():
    lines = EXEMPT_PATH.read_text(encoding="utf-8").splitlines()
    assert lines[0] == (
        "# D-2047 frozen exempt shas: direct runs without spawner run id. Never append."
    )
    for marker in ("#94", "#95", "#96", "#87", "#90"):
        assert marker in lines

    shas = [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(shas) == 33
    assert len(set(shas)) == 33
    for sha in shas:
        assert re.fullmatch(r"[0-9a-f]{40}", sha)
