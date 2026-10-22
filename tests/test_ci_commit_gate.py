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
        for line in lines
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


def test_hand_edit_ten_changed_lines_passes(repo):
    base = _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', {'README.md': 'base\n'})
    sha = _commit(repo, 'docs: add\n\nHand-Edit: add\n', {'docs/x.md': 'l\n' * 10})
    assert check(base, sha, cwd=repo) == []


def test_hand_edit_eleven_changed_lines_refused(repo):
    base = _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', {'README.md': 'base\n'})
    sha = _commit(repo, 'docs: add\n\nHand-Edit: add\n', {'docs/x.md': 'l\n' * 11})
    assert check(base, sha, cwd=repo) == [f'{sha}: hand-edit exceeds 10 changed lines']


def test_hand_edit_deleted_lines_count(repo):
    base = _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', {'docs/y.md': 'l\n' * 20})
    sha = _commit(repo, 'docs: rewrite\n\nHand-Edit: rewrite\n', {'docs/y.md': 'new\n'})
    assert check(base, sha, cwd=repo) == [f'{sha}: hand-edit exceeds 10 changed lines']


def test_root_level_roles_file_hand_edit_refused(repo):
    base = _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', {'README.md': 'base\n'})
    sha = _commit(repo, 'roles: add\n\nHand-Edit: add\n', {'roles.py': 'x\n'})
    assert check(base, sha, cwd=repo) == [f'{sha}: hand-edit touches auth/door surface: roles.py']


def test_gate_file_suffix_copy_needs_gate_edit(repo):
    base = _commit(repo, 'base\n\nWriter: mimo-v2.6-flash run-1\n', {'README.md': 'base\n'})
    sha = _commit(repo, 'add bak\n\nWriter: mimo-v2.6-flash run-1\n', {'tools/ci_commit_gate.py.bak': 'x\n'})
    assert check(base, sha, cwd=repo) == [f'{sha}: gate edit without Gate-Edit trailer']