import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_changelog_docs.py"


def git(cwd, *args, env=None):
    base_env = dict(os.environ)
    base_env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    base_env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    base_env["GIT_CONFIG_NOSYSTEM"] = "1"
    base_env["GIT_AUTHOR_NAME"] = "Test Author"
    base_env["GIT_AUTHOR_EMAIL"] = "author@example.invalid"
    base_env["GIT_COMMITTER_NAME"] = "Test Committer"
    base_env["GIT_COMMITTER_EMAIL"] = "committer@example.invalid"
    if env:
        base_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=base_env,
    )


def commit_files(repo, files: dict[str, str], message: str, trailers: list[str] | None = None) -> str:
    for rel_path, content in files.items():
        p = repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        res = git(repo, "add", rel_path)
        assert res.returncode == 0, f"git add failed: {res.stderr}"
    full_msg = message
    if trailers:
        full_msg = f"{message}\n\n" + "\n".join(trailers) + "\n"
    res = git(repo, "commit", "-m", full_msg)
    assert res.returncode == 0, f"git commit failed: {res.stderr}"
    head = git(repo, "rev-parse", "HEAD")
    return head.stdout.strip()


@pytest.fixture
def test_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    res = git(repo, "init", "-b", "main")
    assert res.returncode == 0
    base = commit_files(repo, {"README.md": "# Readme\n"}, "initial commit")
    return repo, base


def run_check(repo, base=None, *extra_args, env=None):
    cmd = [sys.executable, str(SCRIPT)]
    if base is not None:
        cmd.extend(["--base", base])
    cmd.extend(extra_args)
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=run_env)


def test_code_change_without_changelog_fails(test_repo):
    repo, base = test_repo
    commit_files(repo, {"src/app.py": "print('hello')\n"}, "add app")
    res = run_check(repo, base)
    assert res.returncode == 1
    assert "check-changelog-docs: FAIL" in res.stdout
    assert "changelog no" in res.stdout


def test_code_change_with_changelog_ok(test_repo):
    repo, base = test_repo
    commit_files(repo, {
        "src/app.py": "print('hello')\n",
        "CHANGELOG.md": "## Unreleased\n- Added app\n",
    }, "add app and changelog")
    res = run_check(repo, base)
    assert res.returncode == 0
    assert "check-changelog-docs: ok" in res.stdout
    assert "changelog yes" in res.stdout


def test_docs_only_change_ok(test_repo):
    repo, base = test_repo
    commit_files(repo, {
        "docs/guide.md": "# Guide\n",
        "LICENSE": "MIT\n",
    }, "update docs")
    res = run_check(repo, base)
    assert res.returncode == 0
    assert "check-changelog-docs: ok" in res.stdout
    assert "changelog no" in res.stdout


def test_skip_trailer_with_reason_ok_and_counted(test_repo):
    repo, base = test_repo
    head = commit_files(
        repo,
        {"src/app.py": "print('fixed typo')\n"},
        "fix typo in app",
        trailers=["Changelog: skip (typo fix)"],
    )
    res = run_check(repo, base)
    assert res.returncode == 0
    assert "check-changelog-docs: ok" in res.stdout
    assert "changelog skip" in res.stdout
    assert f"changelog: skip used by {head[:8]} (typo fix)" in res.stdout


def test_skip_trailer_with_empty_reason_fails(test_repo):
    repo, base = test_repo
    commit_files(
        repo,
        {"src/app.py": "print('fixed typo')\n"},
        "fix typo without reason",
        trailers=["Changelog: skip ()"],
    )
    res = run_check(repo, base)
    assert res.returncode == 1
    assert "check-changelog-docs: FAIL" in res.stdout
    assert "changelog no" in res.stdout


def test_docs_map_mapped_path_without_doc_before_enforce_date_warns_exit_0(test_repo):
    repo, base = test_repo
    commit_files(repo, {
        ".docs-map": "# enforce-after: 2026-11-08\nsrc/foo/** -> docs/FOO.md\n",
    }, "add docs map")
    base2 = git(repo, "rev-parse", "HEAD").stdout.strip()
    commit_files(repo, {
        "src/foo/bar.py": "x = 1\n",
        "CHANGELOG.md": "## Unreleased\n- update foo\n",
    }, "update foo")
    res = run_check(repo, base2, "--today", "2026-11-01")
    assert res.returncode == 0
    assert "check-changelog-docs: ok" in res.stdout
    assert "docs-map 1 warning(s)/0 error(s)" in res.stdout
    assert "warning:" in res.stdout


def test_docs_map_mapped_path_without_doc_after_enforce_date_fails(test_repo):
    repo, base = test_repo
    commit_files(repo, {
        ".docs-map": "# enforce-after: 2026-11-08\nsrc/foo/** -> docs/FOO.md\n",
    }, "add docs map")
    base2 = git(repo, "rev-parse", "HEAD").stdout.strip()
    commit_files(repo, {
        "src/foo/bar.py": "x = 1\n",
        "CHANGELOG.md": "## Unreleased\n- update foo\n",
    }, "update foo")
    res = run_check(repo, base2, "--today", "2026-11-08")
    assert res.returncode == 1
    assert "check-changelog-docs: FAIL" in res.stdout
    assert "docs-map 0 warning(s)/1 error(s)" in res.stdout


def test_docs_map_with_doc_ok(test_repo):
    repo, base = test_repo
    commit_files(repo, {
        ".docs-map": "# enforce-after: 2026-11-08\nsrc/foo/** -> docs/FOO.md\n",
    }, "add docs map")
    base2 = git(repo, "rev-parse", "HEAD").stdout.strip()
    commit_files(repo, {
        "src/foo/bar.py": "x = 1\n",
        "docs/FOO.md": "# Foo\n",
        "CHANGELOG.md": "## Unreleased\n- update foo and doc\n",
    }, "update foo and doc")
    res = run_check(repo, base2, "--today", "2026-11-08")
    assert res.returncode == 0
    assert "check-changelog-docs: ok" in res.stdout
    assert "docs-map 0 warning(s)/0 error(s)" in res.stdout


def test_malformed_docs_map_line_fails_with_file_line(test_repo):
    repo, base = test_repo
    commit_files(repo, {
        ".docs-map": "# enforce-after: 2026-11-08\nthis is a malformed rule\n",
        "CHANGELOG.md": "## Unreleased\n- test\n",
    }, "malformed docs map")
    res = run_check(repo, base)
    assert res.returncode == 1
    assert "check-changelog-docs: FAIL" in res.stdout
    out = res.stdout + res.stderr
    assert ".docs-map:2" in out


def test_warn_only_never_nonzero(test_repo):
    repo, base = test_repo
    commit_files(repo, {
        ".docs-map": "# enforce-after: 2026-11-08\nsrc/foo/** -> docs/FOO.md\n",
    }, "add docs map")
    base2 = git(repo, "rev-parse", "HEAD").stdout.strip()
    # Code change without changelog AND mapped path without doc after enforce date
    commit_files(repo, {
        "src/foo/bar.py": "x = 1\n",
    }, "change without changelog or doc")
    res = run_check(repo, base2, "--today", "2026-11-08", "--warn-only")
    assert res.returncode == 0
    assert "check-changelog-docs: ok" in res.stdout
    assert "warning:" in res.stdout

    # Also test base == HEAD with --warn-only
    res_head = run_check(repo, "HEAD", "--warn-only")
    assert res_head.returncode == 0


def test_base_equal_head_refused(test_repo):
    repo, base = test_repo
    res = run_check(repo, "HEAD")
    assert res.returncode == 1
    out = res.stdout + res.stderr
    assert "is HEAD itself" in out
