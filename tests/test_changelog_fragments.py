"""Hermetic tests for changelog.d fragments and tools/changelog_fold.py (D-3732)."""
import os
import subprocess
import sys

import pytest

from tests.changelog_docs_util import ROOT, commit_files, git, run_check, test_repo  # noqa: F401

FOLD = ROOT / "tools" / "changelog_fold.py"
CHANGELOG = "## Unreleased\n- existing entry\n"


def test_tracked_tree_has_no_case_only_duplicate_paths():
    paths = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    folded = {}
    for path in filter(None, paths):
        assert path.casefold() not in folded, (folded.get(path.casefold()), path)
        folded[path.casefold()] = path


def test_slice_fragments_are_consolidated():
    paths = list((ROOT / "changelog.d").glob("*.md"))
    for slice_id in ("s2", "s3", "s4", "s5"):
        fragments = [p for p in paths if p.stem.lower().replace("-", "").startswith("rff" + slice_id)
                     and "s2s5" not in p.stem.lower().replace("-", "")]
        assert len(fragments) <= 1, fragments  # Folding may consume the canonical fragment.
        assert all("remain outstanding" not in p.read_text() for p in fragments)


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


@pytest.mark.parametrize("content,accepted", [
    ("- first entry\n- added entry\n", True),
    (" \n  - first entry  \n", False),
    ("", False),
])
def test_gate_accepts_edited_fragment(test_repo, content, accepted):
    repo, _ = test_repo
    base = commit_files(repo, {
        "changelog.d/PG-EXAMPLE.md": "- first entry\n",
    }, "seed fragment")
    commit_files(repo, {
        "src/app.py": "print('hello')\n",
        "changelog.d/PG-EXAMPLE.md": content,
    }, "edit fragment and app")
    res = run_check(repo, base)
    assert res.returncode == (0 if accepted else 1), res.stdout
    assert f"changelog {'yes' if accepted else 'no'}" in res.stdout


def test_gate_refuses_change_with_neither_fragment_nor_unreleased_line(test_repo):
    repo, base = test_repo
    commit_files(repo, {"src/app.py": "print('hello')\n"}, "add app only")
    res = run_check(repo, base)
    assert res.returncode == 1, res.stdout
    assert "check-changelog-docs: FAIL" in res.stdout
    assert "changelog no" in res.stdout
    assert "requires a line added under '## Unreleased'" in res.stdout


@pytest.mark.parametrize("path,content", [
    ("changelog.d/PG-EMPTY.md", "\n# no entry yet\n"),
    ("changelog.d/PG-SPACE.md", " \n\t\n"),
    ("other/PG-OUTSIDE.md", "- outside entry\n"),
    ("changelog.d/extra/PG.md", "- nested entry\n"),
    ("changelog.d/ReadMe.md", "- readme entry\n"),
])
def test_gate_refuses_empty_fragment(test_repo, path, content):
    repo, base = test_repo
    commit_files(repo, {
        "src/app.py": "print('hello')\n",
        path: content,
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


@pytest.mark.parametrize("content", ["", "# heading\n", " \n\t\n"])
@pytest.mark.parametrize("with_entry", [False, True])
def test_fold_preserves_and_reports_fragments_without_entries(test_repo, content, with_entry):
    repo, _ = test_repo
    repo_with_changelog(repo)
    files = {"changelog.d/PG-EMPTY.md": content}
    if with_entry:
        files["changelog.d/PG-REAL.md"] = "- real entry\n"
    commit_files(repo, files, "seed fragments")
    before = (repo / "CHANGELOG.md").read_bytes()
    checked = run_fold(repo, "--check")
    assert checked.returncode == 1
    assert (repo / "CHANGELOG.md").read_bytes() == before
    assert all((repo / path).exists() for path in files)
    folded = run_fold(repo)
    assert folded.returncode == 0
    assert "no entry lines; leaving changelog.d/PG-EMPTY.md" in folded.stdout
    assert (repo / "changelog.d/PG-EMPTY.md").read_text() == content
    if with_entry:
        assert not (repo / "changelog.d/PG-REAL.md").exists()
        assert "- real entry" in (repo / "CHANGELOG.md").read_text()
    else:
        assert (repo / "CHANGELOG.md").read_bytes() == before
    after = (repo / "CHANGELOG.md").read_bytes()
    assert run_fold(repo).returncode == 0
    assert (repo / "CHANGELOG.md").read_bytes() == after


@pytest.mark.parametrize("state", ["untracked", "staged", "edited"])
def test_fold_uncommitted_fragments_sort_newest_by_mtime_then_name(test_repo, state):
    repo, _ = test_repo
    repo_with_changelog(repo)
    commit_dated(repo, {"changelog.d/old.md": "- committed entry\n"},
                 "committed", "2026-03-01T12:00:00+0000")
    if state == "edited":
        commit_files(repo, {f"changelog.d/{name}.md": "- original\n"
                            for name in ("a", "b", "z")}, "seed edited fragments")
    for name, stamp in [("a", 1), ("b", 2), ("z", 2)]:
        path = repo / "changelog.d" / f"{name}.md"
        path.write_text(f"- {name} entry\n")
        os.utime(path, ns=(stamp, stamp))
        if state == "staged":
            assert git(repo, "add", str(path)).returncode == 0
    assert run_fold(repo).returncode == 0
    text = (repo / "CHANGELOG.md").read_text()
    ordered = ["- b entry", "- z entry", "- a entry", "- committed entry"]
    assert [text.index(entry) for entry in ordered] == sorted(text.index(entry) for entry in ordered)


def test_fold_ignores_nested_outside_and_readme_fragments(test_repo):
    repo, _ = test_repo
    repo_with_changelog(repo)
    files = {path: "- ignored entry\n" for path in (
        "changelog.d/extra/PG.md", "other/PG.md", "changelog.d/ReadMe.md")}
    commit_files(repo, files, "non-fragments")
    assert run_fold(repo, "--check").returncode == 0
    assert run_fold(repo).returncode == 0
    assert (repo / "CHANGELOG.md").read_text() == CHANGELOG
    assert all((repo / path).read_text() == content for path, content in files.items())
