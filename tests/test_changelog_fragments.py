"""Hermetic tests for changelog.d fragments and tools/changelog_fold.py (D-3732)."""
import os
import subprocess
import sys

from tests.changelog_docs_util import ROOT, commit_files, git, run_check, test_repo  # noqa: F401

FOLD = ROOT / "tools" / "changelog_fold.py"
CHANGELOG = "## Unreleased\n- existing entry\n"


def run_fold(repo, *extra_args):
    return subprocess.run(
        [sys.executable, str(FOLD), *extra_args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )


def commit_dated(repo, files, message, when):
    for rel_path, content in files.items():
        path = repo / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        assert git(repo, "add", rel_path).returncode == 0
    env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    res = git(repo, "commit", "-m", message, env=env)
    assert res.returncode == 0, res.stderr
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def repo_with_changelog(repo):
    commit_files(repo, {"CHANGELOG.md": CHANGELOG}, "baseline changelog")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def test_gate_accepts_new_fragment_alone(test_repo):
    repo, base = test_repo
    commit_files(repo, {
        "src/app.py": "print('hello')\n",
        "changelog.d/PG-EXAMPLE.md": "- **PG-EXAMPLE (D-1):** add app\n",
    }, "add app with fragment")
    res = run_check(repo, base)
    assert res.returncode == 0, res.stdout
    assert "check-changelog-docs: ok" in res.stdout
    assert "changelog yes" in res.stdout


def test_gate_accepts_edited_fragment(test_repo):
    repo, _ = test_repo
    base = commit_files(repo, {
        "changelog.d/PG-EXAMPLE.md": "- **PG-EXAMPLE (D-1):** first wording\n",
    }, "seed fragment")
    commit_files(repo, {
        "src/app.py": "print('hello')\n",
        "changelog.d/PG-EXAMPLE.md": "- **PG-EXAMPLE (D-1):** better wording\n",
    }, "edit fragment and app")
    res = run_check(repo, base)
    assert res.returncode == 0, res.stdout
    assert "changelog yes" in res.stdout


def test_gate_refuses_change_with_neither_fragment_nor_unreleased_line(test_repo):
    repo, base = test_repo
    commit_files(repo, {"src/app.py": "print('hello')\n"}, "add app only")
    res = run_check(repo, base)
    assert res.returncode == 1, res.stdout
    assert "check-changelog-docs: FAIL" in res.stdout
    assert "changelog no" in res.stdout
    assert "requires a line added under '## Unreleased'" in res.stdout


def test_gate_refuses_empty_fragment(test_repo):
    repo, base = test_repo
    commit_files(repo, {
        "src/app.py": "print('hello')\n",
        "changelog.d/PG-EMPTY.md": "\n# no entry yet\n",
    }, "add app with empty fragment")
    res = run_check(repo, base)
    assert res.returncode == 1, res.stdout
    assert "changelog no" in res.stdout


def test_two_fragment_branches_merge_cleanly_and_fold_both(test_repo):
    repo, base = test_repo
    base = repo_with_changelog(repo)

    git(repo, "checkout", "-b", "lane-a", base)
    commit_files(repo, {
        "src/a.py": "a = 1\n",
        "changelog.d/PG-A.md": "- **PG-A (D-1):** add a\n",
    }, "lane a")
    assert run_check(repo, base).returncode == 0

    git(repo, "checkout", "-b", "lane-b", base)
    commit_files(repo, {
        "src/b.py": "b = 1\n",
        "changelog.d/PG-B.md": "- **PG-B (D-2):** add b\n",
    }, "lane b")
    assert run_check(repo, base).returncode == 0

    git(repo, "checkout", "main")
    assert git(repo, "merge", "--no-ff", "-m", "merge lane a", "lane-a").returncode == 0
    merged = git(repo, "merge", "--no-ff", "-m", "merge lane b", "lane-b")
    assert merged.returncode == 0, merged.stdout + merged.stderr

    res = run_fold(repo)
    assert res.returncode == 0, res.stdout + res.stderr
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- **PG-A (D-1):** add a" in text
    assert "- **PG-B (D-2):** add b" in text
    assert not (repo / "changelog.d" / "PG-A.md").exists()
    assert not (repo / "changelog.d" / "PG-B.md").exists()


def test_two_unreleased_branches_conflict_without_union_driver(test_repo):
    repo, base = test_repo

    git(repo, "checkout", "-b", "lane-a", base)
    commit_files(repo, {
        "src/a.py": "a = 1\n",
        "CHANGELOG.md": "## Unreleased\n- add a\n",
    }, "lane a line")
    git(repo, "checkout", "-b", "lane-b", base)
    commit_files(repo, {
        "src/b.py": "b = 1\n",
        "CHANGELOG.md": "## Unreleased\n- add b\n",
    }, "lane b line")

    git(repo, "checkout", "main")
    assert git(repo, "merge", "--no-ff", "-m", "merge lane a", "lane-a").returncode == 0
    merged = git(repo, "merge", "--no-ff", "-m", "merge lane b", "lane-b")
    assert merged.returncode != 0
    assert "CONFLICT" in merged.stdout + merged.stderr


def test_fold_orders_newest_first_then_file_name(test_repo):
    repo, _ = test_repo
    repo_with_changelog(repo)
    commit_dated(repo, {"changelog.d/zzz.md": "- old entry\n"}, "old fragment", "2026-02-01T12:00:00+0000")
    commit_dated(repo, {"changelog.d/bbb.md": "- b entry\n"}, "b fragment", "2026-03-01T12:00:00+0000")
    commit_dated(repo, {"changelog.d/aaa.md": "- a entry\n"}, "a fragment", "2026-03-01T12:00:00+0000")

    res = run_fold(repo)
    assert res.returncode == 0, res.stdout + res.stderr
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text.split("## Unreleased", 1)[1]
    assert unreleased.index("- a entry") < unreleased.index("- b entry")
    assert unreleased.index("- b entry") < unreleased.index("- old entry")
    assert unreleased.index("- old entry") < unreleased.index("- existing entry")


def test_fold_deletes_fragments_but_keeps_readme(test_repo):
    repo, _ = test_repo
    repo_with_changelog(repo)
    commit_files(repo, {
        "changelog.d/README.md": "# fragments\n",
        "changelog.d/PG-ONE.md": "- **PG-ONE (D-1):** one\n",
    }, "add fragments")

    assert run_fold(repo).returncode == 0
    assert not (repo / "changelog.d" / "PG-ONE.md").exists()
    assert (repo / "changelog.d" / "README.md").exists()
    assert "- **PG-ONE (D-1):** one" in (repo / "CHANGELOG.md").read_text(encoding="utf-8")


def test_fold_is_idempotent(test_repo):
    repo, _ = test_repo
    repo_with_changelog(repo)
    commit_files(repo, {"changelog.d/PG-ONE.md": "- **PG-ONE (D-1):** one\n"}, "add fragment")

    assert run_fold(repo).returncode == 0
    first = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    second_run = run_fold(repo)
    assert second_run.returncode == 0
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == first


def test_fold_check_writes_nothing(test_repo):
    repo, _ = test_repo
    repo_with_changelog(repo)
    commit_files(repo, {"changelog.d/PG-ONE.md": "- **PG-ONE (D-1):** one\n"}, "add fragment")
    before = (repo / "CHANGELOG.md").read_text(encoding="utf-8")

    res = run_fold(repo, "--check")
    assert res.returncode == 1
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == before
    assert (repo / "changelog.d" / "PG-ONE.md").exists()
