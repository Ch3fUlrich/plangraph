"""Range, trailer-block, CHANGELOG-entry, glob and --head behaviour of check_changelog_docs."""
import pytest

from tests.changelog_docs_util import (  # noqa: F401
    commit_files,
    commit_raw,
    docs_map_repo,
    git,
    run_check,
    test_repo,
)


def test_skip_text_in_body_paragraph_is_not_a_trailer(test_repo):
    repo, base = test_repo
    commit_raw(
        repo,
        {"src/app.py": "x = 1\n"},
        "add feature\n\nChangelog: skip (typo)\n\nThis is a real feature, prose follows.\n",
    )
    res = run_check(repo, base)
    assert res.returncode == 1, res.stdout
    assert "changelog no" in res.stdout
    assert "skip used by" not in res.stdout


def test_skip_trailer_on_one_of_several_commits_does_not_waive_the_range(test_repo):
    repo, base = test_repo
    commit_files(repo, {"src/typo.py": "a = 1\n"}, "fix typo", trailers=["Changelog: skip (typo)"])
    feature = commit_files(repo, {"src/feature.py": "b = 2\n"}, "add feature")
    res = run_check(repo, base)
    assert res.returncode == 1, res.stdout
    assert feature[:8] in res.stdout
    assert "changelog no" in res.stdout


def test_every_non_doc_commit_skipping_is_ok(test_repo):
    repo, base = test_repo
    commit_files(repo, {"src/a.py": "a = 1\n"}, "fix a", trailers=["Changelog: skip (typo)"])
    commit_files(repo, {"src/b.py": "b = 1\n"}, "fix b", trailers=["Changelog: skip (format)"])
    commit_files(repo, {"docs/x.md": "# x\n"}, "docs only")
    res = run_check(repo, base)
    assert res.returncode == 0, res.stdout
    assert "changelog skip" in res.stdout


def test_changelog_entry_in_range_covers_commits_without_skip(test_repo):
    repo, base = test_repo
    commit_files(repo, {"src/a.py": "a = 1\n"}, "add a")
    commit_files(repo, {"CHANGELOG.md": "## Unreleased\n- add a\n"}, "changelog")
    res = run_check(repo, base)
    assert res.returncode == 0, res.stdout
    assert "changelog yes" in res.stdout


def test_merge_commit_in_range_needs_no_skip_of_its_own(test_repo):
    repo, base = test_repo
    git(repo, "checkout", "-b", "feature")
    commit_files(repo, {"src/a.py": "a = 1\n"}, "fix a", trailers=["Changelog: skip (typo)"])
    git(repo, "checkout", "main")
    commit_files(repo, {"docs/n.md": "# n\n"}, "docs on main")
    res = git(repo, "merge", "--no-ff", "-m", "Merge feature", "feature")
    assert res.returncode == 0, res.stderr
    out = run_check(repo, base)
    assert out.returncode == 0, out.stdout
    assert "changelog skip" in out.stdout


def test_skip_trailer_on_a_merge_commit_does_not_waive_a_feature_commit(test_repo):
    repo, base = test_repo
    git(repo, "checkout", "-b", "feature")
    commit_files(repo, {"src/a.py": "a = 1\n"}, "add a")
    git(repo, "checkout", "main")
    commit_files(repo, {"docs/n.md": "# n\n"}, "docs on main")
    res = git(repo, "merge", "--no-ff", "-m", "Merge feature\n\nChangelog: skip (merge)", "feature")
    assert res.returncode == 0, res.stderr
    out = run_check(repo, base)
    assert out.returncode == 1, out.stdout
    assert "changelog no" in out.stdout


def test_empty_and_valid_skip_in_one_commit_reports_both_and_fails(test_repo):
    repo, base = test_repo
    sha = commit_files(
        repo, {"src/a.py": "a = 1\n"}, "fix a",
        trailers=["Changelog: skip ()", "Changelog: skip (ok)"],
    )
    res = run_check(repo, base)
    assert res.returncode == 1, res.stdout
    assert f"changelog: skip used by {sha[:8]} (ok)" in res.stdout
    assert f"commit {sha[:8]} has 'Changelog: skip' trailer with empty reason" in res.stdout


@pytest.mark.parametrize("changelog", [
    "# Changelog\n\n## Unreleased\n\n## 1.0\n- old\n",
    "# Changelog\n\n## Unreleased\n- old\n\n\n",
])
def test_whitespace_only_or_outside_unreleased_changelog_edit_fails(test_repo, changelog):
    repo, base = test_repo
    commit_files(repo, {"CHANGELOG.md": "# Changelog\n\n## Unreleased\n- old\n\n## 1.0\n"}, "seed changelog")
    base2 = git(repo, "rev-parse", "HEAD").stdout.strip()
    commit_files(repo, {"src/a.py": "a = 1\n", "CHANGELOG.md": changelog}, "edit")
    res = run_check(repo, base2)
    assert res.returncode == 1, res.stdout
    assert "changelog no" in res.stdout


def test_deleting_changelog_does_not_satisfy_the_rule(test_repo):
    repo, base = test_repo
    commit_files(repo, {"CHANGELOG.md": "## Unreleased\n- old\n"}, "seed changelog")
    base2 = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert git(repo, "rm", "-q", "CHANGELOG.md").returncode == 0
    commit_files(repo, {"src/a.py": "a = 1\n"}, "drop changelog")
    res = run_check(repo, base2)
    assert res.returncode == 1, res.stdout


def test_double_star_matches_nested_paths(test_repo):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, "src/foo/** -> docs/FOO.md")
    commit_files(repo, {"src/foo/a/b/c.py": "x = 1\n", "CHANGELOG.md": "## Unreleased\n- x\n"}, "nested")
    res = run_check(repo, base2, "--today", "2026-11-08")
    assert res.returncode == 1, res.stdout
    assert "docs-map 0 warning(s)/1 error(s)" in res.stdout


def test_single_star_does_not_cross_a_slash(test_repo):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, "lib/*.py -> docs/LIB.md")
    commit_files(repo, {"lib/sub/x.py": "x = 1\n", "CHANGELOG.md": "## Unreleased\n- x\n"}, "nested")
    res = run_check(repo, base2, "--today", "2026-11-08")
    assert res.returncode == 0, res.stdout
    assert "docs-map 0 warning(s)/0 error(s)" in res.stdout
    commit_files(repo, {"lib/y.py": "y = 1\n"}, "direct")
    res = run_check(repo, base2, "--today", "2026-11-08")
    assert res.returncode == 1, res.stdout


def test_enforcement_boundary_day_before_warns_and_the_day_enforces(test_repo):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, "src/foo/** -> docs/FOO.md")
    commit_files(repo, {"src/foo/a.py": "x = 1\n", "CHANGELOG.md": "## Unreleased\n- x\n"}, "foo")
    before = run_check(repo, base2, "--today", "2026-11-07")
    assert before.returncode == 0 and "docs-map 1 warning(s)/0 error(s)" in before.stdout
    on = run_check(repo, base2, "--today", "2026-11-08")
    assert on.returncode == 1 and "docs-map 0 warning(s)/1 error(s)" in on.stdout


@pytest.mark.parametrize("extra", [(), ("--warn-only",)])
def test_malformed_today_fails_cleanly_without_traceback(test_repo, extra):
    repo, base = test_repo
    commit_files(repo, {"docs/a.md": "# a\n"}, "docs")
    res = run_check(repo, base, "--today", "not-a-date", *extra)
    out = res.stdout + res.stderr
    assert "Traceback" not in out
    assert "not-a-date" in out
    assert res.returncode == (0 if extra else 1)


def test_head_option_checks_a_non_head_ref(test_repo):
    repo, base = test_repo
    git(repo, "checkout", "-b", "other")
    other = commit_files(repo, {"src/o.py": "o = 1\n"}, "other feature")
    git(repo, "checkout", "main")
    commit_files(repo, {"docs/n.md": "# n\n"}, "docs on main")
    assert run_check(repo, base).returncode == 0
    res = run_check(repo, base, "--head", other)
    assert res.returncode == 1, res.stdout
    assert "1 path(s)" in res.stdout


def test_base_not_an_ancestor_uses_the_merge_base_range(test_repo):
    repo, base = test_repo
    git(repo, "checkout", "-b", "feature")
    commit_files(repo, {"docs/f.md": "# f\n"}, "docs on feature")
    git(repo, "checkout", "main")
    tip = commit_files(repo, {"src/m.py": "m = 1\n"}, "code on main")
    git(repo, "checkout", "feature")
    res = run_check(repo, tip)
    assert res.returncode == 0, res.stdout
    assert "1 path(s)" in res.stdout
