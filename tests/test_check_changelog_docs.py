import pytest

from tests.changelog_docs_util import ROOT, commit_files, git, run_check, test_repo  # noqa: F401


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


@pytest.mark.parametrize("path", ["rust/Cargo.toml", "rust/Cargo.lock"])
def test_workspace_docs_map_accepts_workspace_documentation(test_repo, path):
    repo, _ = test_repo
    base = commit_files(repo, {
        ".docs-map": (ROOT / ".docs-map").read_text(),
    }, "use repository docs mapping")
    commit_files(repo, {
        path: "# workspace dependency update\n",
        "CONTRIBUTING.md": "# Contributing\nWorkspace dependency update.\n",
        "CHANGELOG.md": "## Unreleased\n- Update workspace dependencies.\n",
    }, "update workspace and workspace documentation")
    result = run_check(repo, base, "--today", "2026-11-08")
    assert result.returncode == 0, result.stdout
    assert "docs-map 0 warning(s)/0 error(s)" in result.stdout


@pytest.mark.parametrize("path", ["rust/Cargo.toml", "rust/Cargo.lock"])
def test_workspace_docs_map_requires_more_than_feature_documentation(test_repo, path):
    repo, _ = test_repo
    base = commit_files(repo, {
        ".docs-map": (ROOT / ".docs-map").read_text(),
    }, "use repository docs mapping")
    commit_files(repo, {
        path: "# workspace dependency update\n",
        "docs/repo-mirror.md": "# Mirror feature update\n",
        "docs/context-probes.md": "# Probe feature update\n",
        "CHANGELOG.md": "## Unreleased\n- Update workspace dependencies.\n",
    }, "update workspace with only feature documentation")
    result = run_check(repo, base, "--today", "2026-11-08")
    assert result.returncode == 1, result.stdout
    assert "CONTRIBUTING.md" in result.stdout
    assert "docs-map 0 warning(s)/1 error(s)" in result.stdout


def test_writer_docs_map_shared_parity():
    import json
    from pathlib import Path

    from tools.check_changelog_docs import glob_to_regex, parse_docs_map

    cases = json.loads((Path(__file__).parent / "fixtures/writer_docs_map.json").read_text())
    for pattern, path, matches in cases["globs"]:
        assert bool(glob_to_regex(pattern).match(path)) == matches, (pattern, path)
    for text, date, entries, error_classes in cases["maps"]:
        parsed_date, rules, errors = parse_docs_map(text)
        assert (parsed_date.isoformat() if parsed_date else None) == date, text
        assert [[line, glob, docs] for line, glob, docs in rules] == entries, text
        assert [error.split(": ", 1)[1].split(":", 1)[0] for error in errors] == error_classes
