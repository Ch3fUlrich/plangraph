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
    commit_files(repo, {"changelog.d/TEST.md": "- add a\n"}, "changelog")
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
    assert "outside a release" in res.stdout


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
    commit_files(repo, {"src/foo/a/b/c.py": "x = 1\n", "changelog.d/TEST.md": "- x\n"}, "nested")
    res = run_check(repo, base2, "--today", "2026-11-08")
    assert res.returncode == 1, res.stdout
    assert "docs-map 0 warning(s)/1 error(s)" in res.stdout


def test_single_star_does_not_cross_a_slash(test_repo):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, "lib/*.py -> docs/LIB.md")
    commit_files(repo, {"lib/sub/x.py": "x = 1\n", "changelog.d/TEST.md": "- x\n"}, "nested")
    res = run_check(repo, base2, "--today", "2026-11-08")
    assert res.returncode == 0, res.stdout
    assert "docs-map 0 warning(s)/0 error(s)" in res.stdout
    commit_files(repo, {"lib/y.py": "y = 1\n"}, "direct")
    res = run_check(repo, base2, "--today", "2026-11-08")
    assert res.returncode == 1, res.stdout


def test_enforcement_boundary_day_before_warns_and_the_day_enforces(test_repo):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, "src/foo/** -> docs/FOO.md")
    commit_files(repo, {"src/foo/a.py": "x = 1\n", "changelog.d/TEST.md": "- x\n"}, "foo")
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


RULE = "src/foo/** -> docs/FOO.md"
TODAY = ("--today", "2026-11-09")


def _foo_change(repo):
    commit_files(repo, {"src/foo/a.py": "x = 1\n", "changelog.d/TEST.md": "- x\n"}, "foo")


def test_range_deleting_docs_map_still_enforces_the_base_rules(test_repo):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, RULE)
    assert git(repo, "rm", "-q", ".docs-map").returncode == 0
    _foo_change(repo)
    res = run_check(repo, base2, *TODAY)
    assert res.returncode == 1, res.stdout
    assert "docs-map 0 warning(s)/1 error(s)" in res.stdout


def test_range_dropping_a_rule_still_enforces_it(test_repo):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, RULE)
    commit_files(repo, {".docs-map": "# enforce-after: 2026-11-08\nlib/** -> docs/LIB.md\n"}, "drop rule")
    _foo_change(repo)
    res = run_check(repo, base2, *TODAY)
    assert res.returncode == 1, res.stdout
    assert "docs-map 0 warning(s)/1 error(s)" in res.stdout


def test_range_moving_enforce_after_later_still_enforces_from_the_base_date(test_repo):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, RULE)
    commit_files(repo, {".docs-map": f"# enforce-after: 2027-01-01\n{RULE}\n"}, "postpone")
    _foo_change(repo)
    res = run_check(repo, base2, *TODAY)
    assert res.returncode == 1, res.stdout
    assert "docs-map 0 warning(s)/1 error(s)" in res.stdout
    before = run_check(repo, base2, "--today", "2026-11-07")
    assert before.returncode == 0 and "docs-map 1 warning(s)/0 error(s)" in before.stdout


def test_range_removing_enforce_after_line_enforces(test_repo):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, RULE)
    commit_files(repo, {".docs-map": f"{RULE}\n"}, "no date")
    _foo_change(repo)
    res = run_check(repo, base2, "--today", "2026-11-07")
    assert res.returncode == 1, res.stdout


def test_range_adding_a_rule_enforces_it(test_repo):
    repo, base = test_repo
    commit_files(
        repo,
        {".docs-map": f"# enforce-after: 2026-11-08\n{RULE}\n", "src/foo/a.py": "x = 1\n",
         "changelog.d/TEST.md": "- x\n"},
        "add rule and touch it",
    )
    res = run_check(repo, base, *TODAY)
    assert res.returncode == 1, res.stdout
    assert "docs-map 0 warning(s)/1 error(s)" in res.stdout


def test_range_adding_a_second_rule_keeps_base_rule_and_enforces_new_one(test_repo):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, RULE)
    commit_files(
        repo, {".docs-map": f"# enforce-after: 2026-11-08\n{RULE}\nlib/** -> docs/LIB.md\n"}, "add rule"
    )
    commit_files(repo, {"lib/y.py": "y = 1\n", "changelog.d/TEST.md": "- y\n"}, "lib")
    res = run_check(repo, base2, *TODAY)
    assert res.returncode == 1, res.stdout
    assert "docs-map 0 warning(s)/1 error(s)" in res.stdout


def test_uncommitted_docs_map_edit_does_not_change_the_result(test_repo):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, RULE)
    _foo_change(repo)
    (repo / ".docs-map").write_text("# enforce-after: 2099-01-01\n", encoding="utf-8")
    res = run_check(repo, base2, *TODAY)
    assert res.returncode == 1, res.stdout
    assert "docs-map 0 warning(s)/1 error(s)" in res.stdout
    (repo / ".docs-map").unlink()
    assert run_check(repo, base2, *TODAY).returncode == 1


def test_uncommitted_docs_map_is_not_picked_up_either(test_repo):
    repo, base = test_repo
    _foo_change(repo)
    (repo / ".docs-map").write_text(f"# enforce-after: 2026-11-08\n{RULE}\n", encoding="utf-8")
    res = run_check(repo, base, *TODAY)
    assert res.returncode == 0, res.stdout
    assert "docs-map 0 warning(s)/0 error(s)" in res.stdout


def test_docs_map_is_read_at_head_not_the_checked_out_tree(test_repo):
    repo, base = test_repo
    git(repo, "checkout", "-b", "other")
    base2 = docs_map_repo(repo, RULE)
    _foo_change(repo)
    other = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "main")
    res = run_check(repo, base, "--head", other, *TODAY)
    assert res.returncode == 1, res.stdout
    assert "docs-map 0 warning(s)/1 error(s)" in res.stdout


def test_parse_error_names_the_revision(test_repo):
    repo, _ = test_repo
    commit_files(repo, {".docs-map": "# enforce-after: 2026-11-08\nbroken rule\n"}, "bad map at base")
    base2 = git(repo, "rev-parse", "HEAD").stdout.strip()
    commit_files(repo, {".docs-map": "# enforce-after: 2026-11-08\nalso broken\n"}, "bad map at head")
    commit_files(repo, {"docs/a.md": "# a\n"}, "docs")
    res = run_check(repo, base2, *TODAY)
    assert res.returncode == 1, res.stdout
    assert f"(.docs-map at merge-base {base2[:8]})" in res.stdout
    assert "(.docs-map at head " in res.stdout


def _load_module():
    import importlib.util

    from tests.changelog_docs_util import SCRIPT

    spec = importlib.util.spec_from_file_location("check_changelog_docs_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _failing_runner(mod, failing_cmd):
    real = mod.git

    def fake(root, *args):
        if args and args[0] == failing_cmd:
            import subprocess

            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: injected failure")
        return real(root, *args)

    return fake


@pytest.mark.parametrize("failing_cmd", ["rev-list", "diff-tree", "log"])
@pytest.mark.parametrize("warn_only", [False, True])
def test_git_failure_in_commit_enumeration_fails_instead_of_passing(test_repo, monkeypatch, capsys, failing_cmd, warn_only):
    repo, base = test_repo
    commit_files(repo, {"src/a.py": "a = 1\n"}, "feature without changelog")
    mod = _load_module()
    monkeypatch.setattr(mod, "git", _failing_runner(mod, failing_cmd))
    argv = ["--base", base, "--cwd", str(repo)] + (["--warn-only"] if warn_only else [])
    code = mod.main(argv)
    out = capsys.readouterr().out
    assert "injected failure" in out
    assert "check-changelog-docs: ok" not in out
    assert code == (0 if warn_only else 1)


def test_git_failure_reading_docs_map_fails(test_repo, monkeypatch, capsys):
    repo, _ = test_repo
    base2 = docs_map_repo(repo, RULE)
    _foo_change(repo)
    mod = _load_module()
    monkeypatch.setattr(mod, "git", _failing_runner(mod, "ls-tree"))
    code = mod.main(["--base", base2, "--cwd", str(repo), *TODAY])
    out = capsys.readouterr().out
    assert code == 1
    assert "injected failure" in out and ".docs-map at merge-base" in out


@pytest.mark.parametrize("message,allowed", [
    ("docs edit", False),
    ("release\n\nRelease: 1.2.3", True),
    ("release\n\nrelease: maintenance fold", True),
    ("release\n\nRelease:", False),
    ("release\n\nRelease: 1.2.3\n\nBody prose follows.", False),
    ("docs edit\n\nChangelog: skip (docs)", False),
])
def test_direct_changelog_edit_requires_release_trailer(test_repo, message, allowed):
    repo, base = test_repo
    commit_raw(repo, {"CHANGELOG.md": "## Unreleased\n- direct edit\n",
                      "changelog.d/TEST.md": "- fragment does not excuse direct edit\n"}, message)
    result = run_check(repo, base)
    assert result.returncode == (0 if allowed else 1), result.stdout
    if not allowed:
        assert "outside a release" in result.stdout


def test_later_release_does_not_excuse_earlier_direct_edit(test_repo):
    repo, base = test_repo
    bad = commit_files(repo, {"CHANGELOG.md": "- ordinary PR edit\n"}, "bad edit")
    commit_files(repo, {"CHANGELOG.md": "- folded release\n"}, "release", trailers=["Release: 1.0"])
    result = run_check(repo, base)
    assert result.returncode == 1 and bad[:8] in result.stdout


def test_merge_resolution_cannot_bypass_release_rule(test_repo):
    repo, base = test_repo
    git(repo, "checkout", "-b", "feature")
    commit_files(repo, {"docs/a.md": "a\n"}, "feature docs")
    git(repo, "checkout", "main")
    commit_files(repo, {"docs/b.md": "b\n"}, "main docs")
    assert git(repo, "merge", "--no-ff", "--no-commit", "feature").returncode == 0
    commit_files(repo, {"CHANGELOG.md": "- sneaked into merge\n"}, "merge docs")
    result = run_check(repo, base)
    assert result.returncode == 1 and "outside a release" in result.stdout


@pytest.mark.parametrize("resolution,release,passes", [
    ("preserve", False, True), ("ours", False, False),
    ("theirs", False, False), ("drop_common", False, False),
    ("invent", False, False), ("ours", True, True),
])
def test_merge_inherits_release_lines_but_cannot_discard_them(test_repo, resolution, release, passes):
    repo, _ = test_repo
    ledger = "## Unreleased\n- existing\n"
    base = commit_files(repo, {"CHANGELOG.md": ledger, ".gitattributes": "CHANGELOG.md merge=union\n"},
                        "seed", trailers=["Release: seed"])
    git(repo, "checkout", "-b", "feature", base)
    commit_files(repo, {"CHANGELOG.md": ledger + "- feature\n"}, "feature release", ["Release: 1.0"])
    git(repo, "checkout", "main")
    commit_files(repo, {"CHANGELOG.md": ledger + "- main\n"}, "main release", ["Release: 2.0"])
    assert git(repo, "merge", "--no-ff", "--no-commit", "feature").returncode == 0
    merged = (repo / "CHANGELOG.md").read_text()
    assert "- main\n" in merged and "- feature\n" in merged
    if resolution in ("ours", "theirs"):
        git(repo, "checkout", "HEAD" if resolution == "ours" else "feature", "--", "CHANGELOG.md")
    elif resolution == "drop_common":
        (repo / "CHANGELOG.md").write_text(merged.replace("- existing\n", ""))
    elif resolution == "invent":
        (repo / "CHANGELOG.md").write_text(merged + "- invented\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "merge releases" + ("\n\nRelease: 3.0" if release else ""))
    result = run_check(repo, base)
    assert result.returncode == (0 if passes else 1), result.stdout
    if not passes:
        assert "outside a release" in result.stdout


@pytest.mark.parametrize("ending,valid", [
    ("\n\nCo-Authored-By: Claude <claude" "@" "example.invalid>\nClaude-Session: session_test\n", True),
    ("\n\nProse follows the skip.\n\nWriter: codex\n", False),
])
def test_split_attribution_trailer_block(test_repo, ending, valid):
    repo, base = test_repo
    head = commit_raw(
        repo, {"src/app.py": "fixed = True\n"},
        "fix typo\n\nExplain the correction.\n\n"
        "Plan-Item: plangraph:LE-CLAUDE-ATTRIBUTION-SPLITS-TRAILERS\n"
        "Hand-Edit: typo correction\nChangelog: skip (typo correction)" + ending,
    )
    res = run_check(repo, base)
    assert res.returncode == (0 if valid else 1), res.stdout
    if valid:
        assert f"changelog: skip used by {head[:8]} (typo correction)" in res.stdout
    else:
        assert "skip used by" not in res.stdout
        assert head[:8] in res.stdout
