from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "tools" / "ci_commit_gate.py"
EXEMPT_PATH = REPO_ROOT / "tools" / "ci_commit_exempt.txt"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tests.yml"
#: Repo-relative exempt file used by the tamper-proofing tests.
EXEMPT_REL = "tools/ci_commit_exempt.txt"
#: Author/committer address, assembled so this file holds no email-shaped
#: literal (the whole tree is public and scrubbed).
CI_MAIL = "ci" "@" "example.invalid"

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
            "GIT_AUTHOR_EMAIL": CI_MAIL,
            "GIT_COMMITTER_NAME": "plangraph-ci",
            "GIT_COMMITTER_EMAIL": CI_MAIL,
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


@pytest.mark.parametrize("vector", [f"V{i}" for i in range(1, 13)])
def test_open_pr_merge_vectors(repo: Path, tmp_path: Path, monkeypatch, capsys, vector):
    fork = _head(repo)
    _git(repo, "checkout", "-q", "-b", "lane")
    _commit(repo, OK_MSG, files={"lane.txt": "lane\n"})
    _git(repo, "checkout", "-q", "-b", "other", fork)
    other = _commit(repo, OK_MSG, files={"other.txt": "other\n"})
    # V1 models main advancing to B after the lane forked.
    base = other if vector in {"V1", "V7"} else fork
    advanced = _commit(repo, OK_MSG, files={"advance.txt": "advance\n"})
    if vector == "V6":
        base = advanced
    if vector == "V3":
        _git(repo, "checkout", "-q", "-b", "unrelated", fork)
        unrelated = _commit(repo, OK_MSG, files={"unrelated.txt": "unrelated\n"})
    _git(repo, "checkout", "-q", "lane")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge other", other)
    merge = _head(repo)
    heads = tmp_path / "open-heads.txt"
    listed = unrelated if vector == "V3" else advanced
    if vector in {"V2", "V12"}:
        listed = other if vector == "V2" else other + "\n" + merge
    if vector in {"V7", "V8"}:
        _git(repo, "checkout", "-q", "-b", "stacked")
        stacked = _commit(repo, OK_MSG, files={"stacked.txt": "stacked\n"})
        listed = merge + "\n" + stacked + "\n" + advanced
    if vector in {"V9", "V10"}:
        listed = merge
        if vector == "V10":
            _git(repo, "checkout", "-q", "-b", "stacked")
            listed += "\n" + _commit(repo, OK_MSG)
    heads.write_text(listed + "\n", encoding="utf-8")
    args = ["check", "--base", base, "--head", merge]
    if vector != "V4":
        args += ["--open-heads", str(heads)]
    monkeypatch.chdir(repo)
    rc = main(args)
    captured = capsys.readouterr()
    expected = f"{merge}: open_pr_merged {other}\n" if vector in {"V2", "V5", "V8", "V11", "V12"} else ""
    assert rc == (1 if vector in {"V2", "V5", "V8", "V11", "V12"} else 0)
    assert captured.out == expected
    notice = ci_commit_gate.OPEN_PR_SKIP_NOTICE
    assert captured.err == (notice if vector == "V4" else "")


def test_open_heads_help_contract(capsys):
    assert main(["check", "--help"]) == 0
    help_text = " ".join(capsys.readouterr().out.split())
    for phrase in ("PR head SHA", "speculative", "OPEN PRs only", "regenerated per run", "inert", "own head may be listed", "fetch open PR heads first"):
        assert phrase in help_text
    assert "open_pr_merged" in ci_commit_gate.__doc__.splitlines()[0]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_open_heads_octopus_library_and_exempt(repo, tmp_path, encoding):
    base = _head(repo)
    _git(repo, "checkout", "-q", "-b", "lane")
    _commit(repo, OK_MSG, files={"lane.txt": "lane\n"})
    heads = []
    parents = []
    for branch in ("one", "two"):
        _git(repo, "checkout", "-q", "-b", branch, base)
        parents.append(_commit(repo, OK_MSG, files={f"{branch}.txt": branch}))
        heads.append(_commit(repo, OK_MSG))
    _git(repo, "checkout", "-q", "lane")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge peers", *parents)
    merge = _head(repo)
    path = tmp_path / "heads.txt"
    path.write_text("\n" + "\n".join([base, *(sha.upper() for sha in heads)]) + "\n\n", encoding=encoding)
    exempt = tmp_path / "exempt.txt"
    exempt.write_text(merge + "\n", encoding="utf-8")
    assert check(base, merge, cwd=repo, open_heads=path, exempt=exempt) == [
        f"{merge}: open_pr_merged {sha}" for sha in parents
    ]


@pytest.mark.parametrize("command", ["rev-list", "merge-base", "cat-file"])
def test_open_heads_real_git_failure(repo, tmp_path, monkeypatch, capsys, command):
    base = _head(repo)
    head = _commit(repo, OK_MSG)
    path = tmp_path / "heads.txt"
    missing = "0" * 40
    path.write_text((missing if command == "cat-file" else base) + "\n", encoding="utf-8")
    if command == "merge-base":
        head = missing
    elif command == "rev-list":
        base = missing
    monkeypatch.chdir(repo)
    with pytest.raises(ci_commit_gate.GateError, match=f"git {command} failed"):
        check(base, head, cwd=repo, open_heads=path)
    assert main(["check", "--base", base, "--head", head, "--open-heads", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"git {command} failed" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("kind", ["malformed", "short", "missing", "empty"])
def test_open_heads_errors(repo, tmp_path, monkeypatch, capsys, kind):
    path = tmp_path / "heads.txt"
    if kind in {"malformed", "short"}:
        path.write_text("\n" + ("not-a-sha" if kind == "malformed" else "abc1234") + "\n", encoding="utf-8")
    supplied = "" if kind == "empty" else str(path)
    base = _head(repo)
    with pytest.raises(ci_commit_gate.GateError):
        check(base, base, cwd=repo, open_heads=supplied)
    monkeypatch.chdir(repo)
    assert main(["check", "--base", base, "--head", base, "--open-heads", supplied]) == 2
    captured = capsys.readouterr()
    assert "open-heads file" in captured.err
    if kind in {"malformed", "short"}:
        assert ":2:" in captured.err
    assert "skipped" not in captured.err


@pytest.mark.parametrize("kind", ["absent", "tree"])
def test_open_heads_noncommit_reports_line(repo, tmp_path, monkeypatch, capsys, kind):
    base = _head(repo)
    sha = "0" * 40 if kind == "absent" else _git(repo, "rev-parse", f"{base}:").strip()
    path = tmp_path / "heads.txt"
    path.write_text(base + "\n" + sha + "\n", encoding="utf-8")
    with pytest.raises(ci_commit_gate.GateError, match=r"heads.txt:2:.*commit"):
        check(base, base, cwd=repo, open_heads=path)
    monkeypatch.chdir(repo)
    assert main(["check", "--base", base, "--head", base, "--open-heads", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "heads.txt:2:" in captured.err
    assert "commit" in captured.err
    assert "skipped" not in captured.err


def test_open_heads_blank_lines_pass(repo, tmp_path, monkeypatch, capsys):
    base = _head(repo)
    head = _commit(repo, OK_MSG)
    path = tmp_path / "heads.txt"
    path.write_text("\n  \n\t\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert main(["check", "--base", base, "--head", head, "--open-heads", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("at_base", [False, True])
def test_exempt_bom_uppercase_and_blanks(repo, tmp_path, at_base):
    sha = _head(repo)
    text = "\n# exempt\n" + sha.upper() + "\n\n"
    path = repo / EXEMPT_REL if at_base else tmp_path / "exempt.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")
    if at_base:
        base = _commit(repo, OK_MSG)
        assert ci_commit_gate._resolve_exempt(base, EXEMPT_REL, cwd=repo) == {sha}
    else:
        assert load_exempt(path) == {sha}


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


def test_claude_writer_followed_by_clean_writer_still_fails(repo: Path):
    """No line overrides an earlier one: a clean Writer cannot launder a claude one."""
    base = _head(repo)
    message = (
        "feat: two writers\n\n"
        "Writer: claude run-old\n\n"
        "Writer: mimo-v2.6-flash run-1\n"
    )
    sha = _commit(repo, message)
    assert check(base, sha, cwd=repo) == [f"{sha}: claude writer"]


def test_two_clean_writer_lines_pass(repo: Path):
    base = _head(repo)
    message = (
        "feat: two writers\n\n"
        "Writer: mimo-v2.6-flash run-1\n\n"
        "Writer: mimo-v2.6-flash run-2\n"
    )
    sha = _commit(repo, message)
    assert check(base, sha, cwd=repo) == []


def test_lowercase_writer_claude_fails(repo: Path):
    base = _head(repo)
    sha = _commit(repo, "feat: served model\n\nwriter: claude run-1\n")
    assert check(base, sha, cwd=repo) == [f"{sha}: malformed Writer trailer"]


def test_whitespace_only_writer_line_fails(repo: Path):
    base = _head(repo)
    sha = _commit(repo, "feat: blank value\n\nWriter:    \n")
    assert check(base, sha, cwd=repo) == [f"{sha}: malformed Writer trailer"]


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


def test_exempt_list_read_from_base_not_from_pr_edit(repo: Path):
    """A PR that appends its own sha to the exempt file still fails."""
    base = _commit(
        repo,
        "chore: seed exempt file\n\nWriter: mimo-v2.6-flash run-0\n",
        files={EXEMPT_REL: "# frozen: no shas yet\n"},
    )
    sha = _commit(repo, "feat: no trailer at all")
    # The PR adds the violating sha to the working-tree exempt file...
    head = _commit(
        repo,
        "chore: exempt my own commit\n\nWriter: mimo-v2.6-flash run-1\n\nGate-Edit: D-2209\n",
        files={EXEMPT_REL: f"# frozen: no shas yet\n{sha}\n"},
    )
    assert sha in load_exempt(repo / EXEMPT_REL), "working tree really does exempt it"
    # ...but the base copy of the file wins, so the violation stands.
    assert check(base, head, exempt=EXEMPT_REL, cwd=repo) == [
        f"{sha}: missing Writer trailer"
    ]


def test_exempt_bootstrap_uses_working_tree_when_absent_in_base(repo: Path):
    """Bootstrap self-exempt removed; absent at base means no exemptions (D-2209)."""
    base = _head(repo)  # no exempt file at base: bootstrap PR
    sha = _commit(repo, "feat: no trailer at all")
    head = _commit(
        repo,
        "chore: introduce exempt file\n\nWriter: mimo-v2.6-flash run-1\n\nGate-Edit: D-2209\n",
        files={EXEMPT_REL: f"{sha}\n"},
    )
    assert check(base, head, exempt=EXEMPT_REL, cwd=repo) == [
        f"{sha}: missing Writer trailer"
    ]


def test_deleted_test_file_without_trailer_fails(repo: Path):
    _commit(
        repo,
        "test: add coverage\n\nWriter: mimo-v2.6-flash run-1\n",
        files={"tests/test_x.py": "def test_alpha():\n    pass\n"},
    )
    base = _head(repo)
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


def test_lowercase_test_removal_trailer_passes(repo: Path):
    """The trailer key is case-insensitive, like ``writer:``."""
    base = _head(repo)
    _commit(
        repo,
        "test: add coverage\n\nWriter: mimo-v2.6-flash run-1\n",
        files={"tests/test_x.py": "def test_alpha():\n    pass\n"},
    )
    sha = _commit(
        repo,
        "chore: drop coverage\n\nWriter: mimo-v2.6-flash run-2\n\n"
        "test-removal: D-1\n",
        remove=("tests/test_x.py",),
    )
    assert check(base, sha, cwd=repo) == []


def test_test_removal_trailer_with_trailing_junk_does_not_satisfy(repo: Path):
    _commit(
        repo,
        "test: add coverage\n\nWriter: mimo-v2.6-flash run-1\n",
        files={"tests/test_x.py": "def test_alpha():\n    pass\n"},
    )
    base = _head(repo)
    sha = _commit(
        repo,
        "chore: drop coverage\n\nWriter: mimo-v2.6-flash run-2\n\n"
        "Test-Removal: D-12x\n",
        remove=("tests/test_x.py",),
    )
    assert check(base, sha, cwd=repo) == [
        f"{sha}: test removal without Test-Removal trailer"
    ]


def test_deleted_widened_test_paths_detected(repo: Path):
    """Nested tests/sub/test_y.py and a top-level foo_test.py are both test files."""
    _commit(
        repo,
        "test: seed\n\nWriter: mimo-v2.6-flash run-1\n",
        files={
            "tests/sub/test_y.py": "def test_nested():\n    pass\n",
            "foo_test.py": "def test_foo():\n    pass\n",
        },
    )
    base = _head(repo)
    sha = _commit(
        repo,
        "chore: drop coverage\n\nWriter: mimo-v2.6-flash run-2\n",
        remove=("tests/sub/test_y.py", "foo_test.py"),
    )
    assert check(base, sha, cwd=repo) == [
        f"{sha}: test removal without Test-Removal trailer"
    ]


def test_removed_def_line_in_widened_test_path_detected(repo: Path):
    _commit(
        repo,
        "test: seed\n\nWriter: mimo-v2.6-flash run-1\n",
        files={"foo_test.py": "def test_foo():\n    pass\n"},
    )
    base = _head(repo)
    sha = _commit(
        repo,
        "refactor: drop the test body\n\nWriter: mimo-v2.6-flash run-2\n",
        files={"foo_test.py": "def helper():\n    pass\n"},
    )
    assert check(base, sha, cwd=repo) == [
        f"{sha}: test removal without Test-Removal trailer"
    ]


def test_removed_def_line_in_non_test_file_is_ignored(repo: Path):
    """The whole-commit diff only counts removals inside test-shaped files."""
    base = _head(repo)
    _commit(
        repo,
        "feat: seed\n\nWriter: mimo-v2.6-flash run-1\n",
        files={"src/app.py": "def test_fake():\n    pass\n"},
    )
    sha = _commit(
        repo,
        "refactor: drop the helper\n\nWriter: mimo-v2.6-flash run-2\n",
        files={"src/app.py": "def other():\n    pass\n"},
    )
    assert check(base, sha, cwd=repo) == []


def test_removed_test_function_without_trailer_fails(repo: Path):
    _commit(
        repo,
        "test: seed\n\nWriter: mimo-v2.6-flash run-1\n",
        files={"tests/test_y.py": "def test_beta():\n    pass\n"},
    )
    base = _head(repo)
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


def test_bad_exempt_line_at_base_exits_2(repo: Path, monkeypatch, capsys):
    base = _commit(
        repo,
        "chore: seed exempt file\n\nWriter: mimo-v2.6-flash run-0\n",
        files={EXEMPT_REL: "# comment\n\nnot-a-sha\n"},
    )
    sha = _commit(repo, OK_MSG)

    monkeypatch.chdir(repo)
    ret = main(["check", "--base", base, "--head", sha, "--exempt", EXEMPT_REL])
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


def test_cli_default_exempt_is_repo_relative():
    """The CLI default must be the repo-relative path, base-anchored like --exempt."""
    assert ci_commit_gate.DEFAULT_EXEMPT == "tools/ci_commit_exempt.txt"
    assert not Path(ci_commit_gate.DEFAULT_EXEMPT).is_absolute()


def test_cli_default_exempt_resolves_from_base_not_from_pr_edit(repo: Path, monkeypatch, capsys):
    """With no --exempt, a PR appending its own sha to the default file still fails."""
    base = _commit(
        repo,
        "chore: seed exempt file\n\nWriter: mimo-v2.6-flash run-0\n",
        files={EXEMPT_REL: "# frozen: no shas yet\n"},
    )
    sha = _commit(repo, "feat: no trailer at all")
    head = _commit(
        repo,
        "chore: exempt my own commit\n\nWriter: mimo-v2.6-flash run-1\n\nGate-Edit: D-2209\n",
        files={EXEMPT_REL: f"# frozen: no shas yet\n{sha}\n"},
    )
    assert sha in load_exempt(repo / EXEMPT_REL), "working tree really does exempt it"

    monkeypatch.chdir(repo)  # CI case: cwd is the repo root, no --exempt given
    ret = main(["check", "--base", base, "--head", head])
    captured = capsys.readouterr()
    assert ret == 1
    assert captured.out.strip() == f"{sha}: missing Writer trailer"


def test_cli_check_ok_prints_nothing(repo: Path, monkeypatch, capsys):
    base = _commit(
        repo,
        "chore: seed exempt file\n\nWriter: mimo-v2.6-flash run-0\n",
        files={EXEMPT_REL: "# frozen: no shas yet\n"},
    )
    sha = _commit(repo, OK_MSG)
    monkeypatch.chdir(repo)
    # No --exempt: the repo-relative default resolves from base and loads cleanly.
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
        for line in lines[:next((i for i, x in enumerate(lines) if x.startswith("# D-2744 ")), len(lines))]
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(shas) == 33
    assert len(set(shas)) == 33
    for sha in shas:
        assert re.fullmatch(r"[0-9a-f]{40}", sha)


def test_workflow_commit_trailers_step_runs_always_on_pull_request():
    """The ci-gate trailer step must run even when earlier steps failed."""
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["ci-gate"]["steps"]
    trailer_steps = [s for s in steps if s.get("name") == "Commit trailers"]
    assert len(trailer_steps) == 1
    expr = trailer_steps[0].get("if") or ""
    assert "always()" in expr
    assert "pull_request" in expr
    assert "push" in expr


def test_pr_editing_gate_is_refused_by_base_copy(repo: Path, tmp_path: Path):
    base = _commit(
        repo,
        OK_MSG,
        files={'tools/ci_commit_gate.py': MODULE_PATH.read_text(encoding='utf-8')},
    )
    head = _commit(
        repo,
        'feat: weaken gate',
        files={'tools/ci_commit_gate.py': 'import sys\nsys.exit(0)\n'},
    )
    gate_base = tmp_path / 'gate_base.py'
    gate_base.write_text(
        _git(repo, 'show', f'{base}:tools/ci_commit_gate.py'),
        encoding='utf-8',
    )
    proc = subprocess.run(
        [sys.executable, str(gate_base), 'check', '--base', base, '--head', head],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert f'{head}: missing Writer trailer' in proc.stdout
    assert 'gate edit without Gate-Edit trailer' in proc.stdout
    proc = subprocess.run(
        [sys.executable, str(repo / 'tools/ci_commit_gate.py'), 'check', '--base', base, '--head', head],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0


def test_strict_writer_leading_whitespace_refused(repo: Path):
    base = _head(repo)
    sha = _commit(
        repo,
        'feat: x\n\n  Writer: mimo-v2.6-flash run-1\n',
        files={'a.txt': 'x'},
    )
    assert check(base, sha, cwd=repo) == [f'{sha}: malformed Writer trailer']


def test_strict_writer_space_before_colon_refused(repo: Path):
    base = _head(repo)
    sha = _commit(
        repo,
        'feat: x\n\nWriter : mimo-v2.6-flash run-1\n',
        files={'a.txt': 'x'},
    )
    assert check(base, sha, cwd=repo) == [f'{sha}: malformed Writer trailer']


def test_hand_edit_without_writer_passes(repo: Path):
    base = _head(repo)
    sha = _commit(
        repo,
        'fix: tweak\n\nHand-Edit: typo in docs\n',
        files={'docs/x.md': 'x'},
    )
    assert check(base, sha, cwd=repo) == []


def test_hand_edit_on_auth_surface_refused(repo: Path):
    base = _head(repo)
    sha = _commit(
        repo,
        'fix: tweak\n\nHand-Edit: typo in docs\n',
        files={'src/plangraph/keeper/mcp.py': 'x'},
    )
    assert check(base, sha, cwd=repo) == [
        f'{sha}: hand-edit touches auth/door surface: src/plangraph/keeper/mcp.py'
    ]


def test_hand_edit_commits_listed_on_stderr(repo: Path, monkeypatch, capsys):
    base = _head(repo)
    sha = _commit(
        repo,
        'fix: tweak\n\nHand-Edit: typo in docs\n',
        files={'docs/x.md': 'x'},
    )
    monkeypatch.chdir(repo)
    main(['check', '--base', base, '--head', sha])
    err = capsys.readouterr().err
    assert f'hand-edit {sha}: typo in docs' in err
    assert ci_commit_gate.hand_edit_commits(base, sha, cwd=repo) == [(sha, 'typo in docs')]


def test_js_test_removal_needs_trailer(repo: Path):
    base = _commit(
        repo,
        OK_MSG,
        files={'tests/foo.test.js': '''it('does x', () => {});\n'''},
    )
    sha = _commit(
        repo,
        'chore: remove test\n\nWriter: mimo-v2.6-flash run-1\n',
        remove=('tests/foo.test.js',),
    )
    assert check(base, sha, cwd=repo) == [f'{sha}: test removal without Test-Removal trailer']

    base = _commit(
        repo,
        OK_MSG,
        files={'tests/foo.test.js': '''it('does x', () => {});\n'''},
    )
    sha = _commit(
        repo,
        'chore: remove test\n\nWriter: mimo-v2.6-flash run-1\n\nTest-Removal: D-1\n',
        remove=('tests/foo.test.js',),
    )
    assert check(base, sha, cwd=repo) == []


def test_bats_test_removal_needs_trailer(repo: Path):
    base = _commit(
        repo,
        OK_MSG,
        files={'tests/t.bats': '@test ' + chr(34) + 'works' + chr(34) + ' {\n'},
    )
    sha = _commit(
        repo,
        'chore: remove bats test\n\nWriter: mimo-v2.6-flash run-1\n',
        remove=('tests/t.bats',),
    )
    assert check(base, sha, cwd=repo) == [f'{sha}: test removal without Test-Removal trailer']


def test_test_added_in_pr_then_removed_is_not_removal(repo: Path):
    base = _head(repo)
    _commit(
        repo,
        OK_MSG,
        files={'tests/test_new.py': 'def test_x():\n    pass\n'},
    )
    sha = _commit(
        repo,
        'chore: drop new test\n\nWriter: mimo-v2.6-flash run-1\n',
        remove=('tests/test_new.py',),
    )
    assert check(base, sha, cwd=repo) == []

    base = _head(repo)
    _commit(
        repo,
        OK_MSG,
        files={'tests/new.test.js': '''it('x', () => {});\n'''},
    )
    sha = _commit(
        repo,
        'chore: drop new js test\n\nWriter: mimo-v2.6-flash run-1\n',
        remove=('tests/new.test.js',),
    )
    assert check(base, sha, cwd=repo) == []


def test_changed_signature_is_not_removal(repo: Path):
    base = _commit(
        repo,
        OK_MSG,
        files={'tests/test_sig.py': 'def test_a(x):\n    pass\n'},
    )
    sha = _commit(
        repo,
        'chore: change signature\n\nWriter: mimo-v2.6-flash run-1\n',
        files={'tests/test_sig.py': 'def test_a(x, y):\n    pass\n'},
    )
    assert check(base, sha, cwd=repo) == []


def test_renamed_base_test_is_removal(repo: Path):
    base = _commit(
        repo,
        OK_MSG,
        files={'tests/test_z.py': 'def test_old():\n    pass\n'},
    )
    sha = _commit(
        repo,
        'chore: rename test\n\nWriter: mimo-v2.6-flash run-1\n',
        files={'tests/test_z.py': 'def test_new():\n    pass\n'},
    )
    assert check(base, sha, cwd=repo) == [f'{sha}: test removal without Test-Removal trailer']


def test_gate_edit_required_for_workflow_and_exempt(repo: Path):
    base = _head(repo)
    sha = _commit(
        repo,
        'chore: edit workflow\n\nWriter: mimo-v2.6-flash run-1\n',
        files={'.github/workflows/x.yml': 'name: x\n'},
    )
    assert check(base, sha, cwd=repo) == [f'{sha}: gate edit without Gate-Edit trailer']

    base = _head(repo)
    sha = _commit(
        repo,
        'chore: edit workflow\n\nWriter: mimo-v2.6-flash run-1\n\nGate-Edit: D-2209\n',
        files={'.github/workflows/x.yml': 'name: y\n'},
    )
    assert check(base, sha, cwd=repo) == []

    base = _head(repo)
    sha = _commit(
        repo,
        'chore: edit workflow\n\nWriter: mimo-v2.6-flash run-1\n\nGate-Edit: D-x\n',
        files={'.github/workflows/x.yml': 'name: z\n'},
    )
    assert check(base, sha, cwd=repo) == [f'{sha}: gate edit without Gate-Edit trailer']


def test_workflow_runs_gate_from_base_copy_and_on_push():
    text = WORKFLOW_PATH.read_text(encoding='utf-8')
    workflow = yaml.safe_load(text)
    steps = workflow['jobs']['ci-gate']['steps']
    trailer = [s for s in steps if s.get('name') == 'Commit trailers'][0]
    run = trailer['run']
    assert 'git show' in run
    assert 'ci_commit_gate_base.py' in run
    assert 'python3 tools/ci_commit_gate.py' not in run
    on = workflow.get('on') or workflow.get(True)
    assert 'push' in on


def test_hand_edit_does_not_satisfy_gate_edit(repo: Path):
    base = _head(repo)
    sha = _commit(
        repo,
        'fix: tweak\n\nHand-Edit: typo\n',
        files={'.github/workflows/x.yml': 'name: x\n'},
    )
    assert check(base, sha, cwd=repo) == [
        f'{sha}: hand-edit touches auth/door surface: .github/workflows/x.yml',
        f'{sha}: gate edit without Gate-Edit trailer'
    ]


def test_hand_edit_blank_value_is_malformed(repo: Path):
    base = _head(repo)
    sha = _commit(
        repo,
        'fix: tweak\n\nHand-Edit:   \n',
        files={'docs/y.md': 'x'},
    )
    assert check(base, sha, cwd=repo) == [
        f'{sha}: missing Writer trailer',
        f'{sha}: malformed Hand-Edit trailer'
    ]


def test_strict_writer_tab_variants_refused(repo: Path):
    base = _head(repo)
    sha = _commit(
        repo,
        'feat: x\n\nWriter:\tmimo-v2.6-flash run-1\n',
        files={'a.txt': 'x'},
    )
    assert check(base, sha, cwd=repo) == [
        f'{sha}: malformed Writer trailer'
    ]
    base = _head(repo)
    sha = _commit(
        repo,
        'feat: x\n\n\tWriter: mimo-v2.6-flash run-1\n',
        files={'b.txt': 'x'},
    )
    assert check(base, sha, cwd=repo) == [
        f'{sha}: malformed Writer trailer'
    ]


def test_workflow_yaml_extension_needs_gate_edit(repo: Path):
    base = _head(repo)
    sha = _commit(
        repo,
        'chore: wf\n\nWriter: mimo-v2.6-flash run-1\n',
        files={'.github/workflows/y.yaml': 'name: y\n'},
    )
    assert check(base, sha, cwd=repo) == [
        f'{sha}: gate edit without Gate-Edit trailer'
    ]


def test_gate_wiring_check_requires_gate_edit(repo: Path):
    base = _head(repo)
    sha = _commit(repo, 'feat: x\n\nWriter: mimo-v2.6-flash run-1\n', files={'tools/gate_wiring_check.py': 'x'})
    assert check(base, sha, cwd=repo) == [f'{sha}: gate edit without Gate-Edit trailer']


def test_hand_edit_auth_path_rename_refused(repo):
    _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', {'src/plangraph/keeper/mcp.py': 'x\n'})
    base = _head(repo)
    (repo / 'docs').mkdir()
    _git(repo, 'mv', 'src/plangraph/keeper/mcp.py', 'docs/mcp_moved.py')
    sha = _commit(repo, 'fix: move\n\nHand-Edit: move\n')
    violations = check(base, sha, cwd=repo)
    assert f'{sha}: hand-edit touches auth/door surface: src/plangraph/keeper/mcp.py' in violations


def test_hand_edit_fifty_changed_lines_passes(repo):
    base = _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', {'README.md': 'base\n'})
    sha = _commit(repo, 'docs: add\n\nHand-Edit: add\n', {'docs/x.md': 'l\n' * 50})
    assert check(base, sha, cwd=repo) == []


def test_hand_edit_fifty_one_changed_lines_refused(repo):
    base = _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', {'README.md': 'base\n'})
    sha = _commit(repo, 'docs: add\n\nHand-Edit: add\n', {'docs/x.md': 'l\n' * 401})
    violations = check(base, sha, cwd=repo)
    assert any('hand-edit exceeds 400 changed lines' in v for v in violations)
    assert any('hand-edit total over ceiling for (none)' in v for v in violations)


def test_hand_edit_deleted_lines_count(repo):
    base = _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', {'docs/y.md': 'l\n' * 500})
    sha = _commit(repo, 'docs: rewrite\n\nHand-Edit: rewrite\n', {'docs/y.md': 'new\n'})
    violations = check(base, sha, cwd=repo)
    assert any('hand-edit exceeds 400 changed lines' in v for v in violations)


def test_root_level_roles_file_hand_edit_refused(repo):
    base = _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', {'README.md': 'base\n'})
    sha = _commit(repo, 'roles: add\n\nHand-Edit: add\n', {'roles.py': 'x\n'})
    assert check(base, sha, cwd=repo) == [f'{sha}: hand-edit touches auth/door surface: roles.py']


def test_gate_file_suffix_copy_needs_gate_edit(repo):
    base = _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', {'README.md': 'base\n'})
    sha = _commit(repo, 'add bak\n\nWriter: mimo-v2.6-flash run-1\n', {'tools/ci_commit_gate.py.bak': 'x\n'})
    assert check(base, sha, cwd=repo) == [f'{sha}: gate edit without Gate-Edit trailer']

def test_hand_edit_auth_path_rename_exact(repo: Path) -> None:
    _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', files={'src/plangraph/keeper/mcp.py': 'x\n'})
    base = _head(repo)
    (repo / 'docs').mkdir()
    _git(repo, 'mv', 'src/plangraph/keeper/mcp.py', 'docs/mcp_moved.py')
    sha = _commit(repo, 'fix: move\n\nHand-Edit: move\n')
    assert check(base, sha, cwd=repo) == [f'{sha}: hand-edit touches auth/door surface: src/plangraph/keeper/mcp.py']


@pytest.mark.parametrize("refuse", [False, True])
def test_autoos_run_id_flagged_or_refused(repo: Path, monkeypatch, capsys, refuse):
    # D-2749: AutoOS run ids are flagged; --refuse-autoos fails them; plangraph run ids pass.
    base = _head(repo)
    autoos = _commit(repo, "feat: a\n\nWriter: gemini-3.8-flash 20261025-092523-stall-quiet-fix1-bcc9ea")
    head = _commit(repo, "feat: b\n\nWriter: mimo/mimo-v2.6-pro run-" + "0123456789abcdef" * 2)
    assert ci_commit_gate.autoos_spawn_commits(base, head, cwd=repo) == [
        (autoos, "gemini-3.8-flash 20261025-092523-stall-quiet-fix1-bcc9ea")]
    monkeypatch.chdir(repo)
    argv = ["check", "--base", base, "--head", head, "--exempt", str(repo / "none.txt")]
    (repo / "none.txt").write_text("", encoding="utf-8")
    code = main(argv + (["--refuse-autoos"] if refuse else []))
    out = capsys.readouterr()
    if refuse:
        assert code == 1 and f"{autoos}: autoos spawner run id" in out.out
    else:
        assert code == 0 and f"autoos-spawn {autoos}:" in out.err


# ---------------------------------------------------------------------------
# E1a gate tests (D-2835, D-2853, crit 2, invariant IE1)
# ---------------------------------------------------------------------------
VALID_ROUTE_DECISION = "Route-Decision: route-" + "a" * 32
_he_counter = 0


def _he(repo: Path, n: int, item: str | None = None, rd: bool = False,
        path: str | None = None, extra: str = "") -> str:
    global _he_counter
    _he_counter += 1
    tr = "\nHand-Edit: edit\n"
    if item:
        tr += f"Plan-Item: {item}\n"
    if rd:
        tr += f"{VALID_ROUTE_DECISION}\n"
    tr += extra
    p = path or f"docs/f{n}_{_he_counter}.md"
    return _commit(repo, f"docs: edit\n{tr}", {p: "x\n" * n})


def test_e1_hand_edits_400_lines_pass_401_refused_door_refused(repo: Path):
    base = _head(repo)
    s400 = _he(repo, 400, rd=True)
    assert check(base, s400, cwd=repo) == []
    s401 = _he(repo, 401, rd=True)
    assert any("hand-edit exceeds 400 changed lines" in v for v in check(s400, s401, cwd=repo))
    sdoor = _he(repo, 30, path="tools/seat_verdict.py")
    assert any("hand-edit touches auth/door surface: tools/seat_verdict.py" in v for v in check(s401, sdoor, cwd=repo))


def test_e1_route_decision_threshold_50_51_lines(repo: Path):
    base = _head(repo)
    s50 = _he(repo, 50)
    assert check(base, s50, cwd=repo) == []
    s51_no = _he(repo, 51)
    assert any("without Route-Decision" in v for v in check(s50, s51_no, cwd=repo))
    s51_bad = _he(repo, 51, extra="Route-Decision: route-short\n")
    assert any("without Route-Decision" in v for v in check(s51_no, s51_bad, cwd=repo))
    s51_two = _he(repo, 51, extra=f"{VALID_ROUTE_DECISION}\n{VALID_ROUTE_DECISION}\n")
    assert any("without Route-Decision" in v for v in check(s51_bad, s51_two, cwd=repo))
    s51_ok = _he(repo, 51, rd=True)
    assert check(s51_two, s51_ok, cwd=repo) == []


def test_e1_history_running_sum_route_decision(repo: Path):
    base = _head(repo)
    c1 = _he(repo, 40, item="demo:ITEM")
    assert check(base, c1, cwd=repo) == []
    c2_no = _he(repo, 20, item="demo:ITEM")
    assert check(c1, c2_no, cwd=repo) == [f"{c2_no}: hand-edit over 50 lines for demo:ITEM without Route-Decision"]
    _git(repo, "reset", "--hard", c1)
    c2_ok = _he(repo, 20, item="demo:ITEM", rd=True)
    assert check(c1, c2_ok, cwd=repo) == []
    c3_no = _he(repo, 5, item="demo:ITEM")
    assert check(c2_ok, c3_no, cwd=repo) == [f"{c3_no}: hand-edit over 50 lines for demo:ITEM without Route-Decision"]


def test_e1_hand_edits_secret_paths_refused(repo: Path):
    base = _head(repo)
    s1 = _he(repo, 1, path="vault/x.json")
    assert check(base, s1, cwd=repo) == [f"{s1}: hand-edit touches secret-shaped path: vault/x.json"]
    s2 = _he(repo, 1, path="config/.env")
    assert check(s1, s2, cwd=repo) == [f"{s2}: hand-edit touches secret-shaped path: config/.env"]


def test_e1_canonical_and_malformed_paths(repo: Path, monkeypatch):
    base = _head(repo)
    f = repo / "src\\plangraph\\keeper\\x.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs: backslash door\n\nHand-Edit: backslash\n")
    s_bs = _head(repo)
    assert check(base, s_bs, cwd=repo) == [f"{s_bs}: hand-edit touches auth/door surface: src\\plangraph\\keeper\\x.py"]
    s_dot = _he(repo, 1)
    monkeypatch.setattr(ci_commit_gate, "_touched_paths", lambda sha, cwd=None: ["./src/plangraph/keeper/x.py"])
    assert check(s_bs, s_dot, cwd=repo) == [f"{s_dot}: hand-edit touches auth/door surface: ./src/plangraph/keeper/x.py"]
    s_dotdot = _he(repo, 1)
    monkeypatch.setattr(ci_commit_gate, "_touched_paths", lambda sha, cwd=None: ["../x.py"])
    assert check(s_dot, s_dotdot, cwd=repo) == [f"{s_dotdot}: hand-edit touches malformed path: ../x.py"]


def test_e1_range_ceiling_bucket_sum(repo: Path):
    base = _head(repo)
    c1 = _he(repo, 210, item="demo:ITEM", rd=True)
    c2 = _he(repo, 210, item="demo:ITEM", rd=True)
    assert check(base, c2, cwd=repo) == [f"{c2}: hand-edit total over ceiling for demo:ITEM"]
    _git(repo, "reset", "--hard", base)
    c1_two = _he(repo, 210, item="demo:ITEM_A", rd=True)
    c2_two = _he(repo, 210, item="demo:ITEM_B", rd=True)
    assert check(base, c2_two, cwd=repo) == []


def test_e1_check_vectors(repo: Path):
    base = _head(repo)
    s_bad = _commit(repo, "docs: typo\n\nhand-edit: oops\n", {"docs/oops.md": "l\n"})
    v = check(base, s_bad, cwd=repo)
    assert f"{s_bad}: malformed Hand-Edit trailer" in v and f"{s_bad}: missing Writer trailer" in v
    s_w = _commit(repo, "feat: w\n\nWriter: mimo-v2.6-flash r1\nPlan-Item: demo:ITEM\n", {"src/x.py": "l\n" * 300})
    s_h = _he(repo, 200, item="demo:ITEM", rd=True)
    assert check(s_bad, s_h, cwd=repo) == []
    s_body = _commit(repo, f"docs: 60 lines\n\nBody text.\nRoute-Decision: route-{'a'*32}\n\nHand-Edit: e\n", {"docs/b.md": "l\n" * 60})
    assert any("without Route-Decision" in x for x in check(s_h, s_body, cwd=repo))
    assert all(ci_commit_gate.canon_path(p) is None for p in ("/etc/passwd", "../outside.py", "foo/../../bar.py"))
    assert ci_commit_gate.canon_path("./a/b/c.py") == "a/b/c.py" and ci_commit_gate.canon_path("a\\b\\c.py") == "a/b/c.py"
    proc = subprocess.run([sys.executable, str(MODULE_PATH), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0 and "usage:" in proc.stdout


def test_e1_drift_and_shared_fixture():
    from plangraph.gates import doors, privacy
    assert ci_commit_gate.AUTH_DOOR_PATHS == doors.AUTH_DOOR_PATHS
    assert ci_commit_gate.HAND_EDIT_MAX_LINES == doors.HAND_EDIT_MAX_LINES == 400
    assert ci_commit_gate.ROUTE_DECISION_LINES == doors.ROUTE_DECISION_LINES == 50
    assert ci_commit_gate.SECRET_NAME_PATTERNS == doors.SECRET_NAME_PATTERNS == privacy.SECRET_NAME_PATTERNS
    assert ci_commit_gate.SECRET_DIRS == doors.SECRET_DIRS == privacy.SECRET_DIRS
    assert len(doors.FIXTURE_ROWS) >= 40
    for path, exp_gate, exp_route in doors.FIXTURE_ROWS:
        g = ci_commit_gate.gate_path_verdict(path)
        d = doors.blocked_path(path)
        assert g == exp_gate and d == exp_gate, f"mismatch on {path}"
        r = "writer" if exp_gate is not None else "direct_edit"
        assert r == exp_route, f"route mismatch on {path}"

