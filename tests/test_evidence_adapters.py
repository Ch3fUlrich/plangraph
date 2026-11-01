"""Tests for the real evidence adapters (local git only, no network)."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from git_helpers import git_cli, git_supports_init_branch
from plangraph.keeper import evidence_ci
from plangraph.keeper.evidence_ci import CIAdapter
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.evidence_git import GitAdapter, parse_trailers
from plangraph.keeper.evidence_runs import RunRecordAdapter
from plangraph.keeper.evidence_semaphore import SemaphoreAdapter, SemaphoreError


# --------------------------------------------------------------------------- #
# RunRecordAdapter (filesystem, path traversal)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("run_id,ok", [
    ("run-1", True),
    ("2026.10.01_run", True),
    ("..", False),
    (".", False),
    ("../etc/passwd", False),
    ("a/b", False),
    ("", False),
    ("a b", False),
    ("a;rm", False),
    (None, False),
])
def test_run_record_valid_run_id(run_id, ok):
    assert RunRecordAdapter("/runs").valid_run_id(run_id) is ok


def test_run_record_reads_job_and_exit(tmp_path):
    run = tmp_path / "run-1"
    run.mkdir()
    (run / "job.json").write_text(json.dumps({"request": {"title": "do x"}, "task": "t"}), encoding="utf-8")
    (run / "exit.json").write_text(json.dumps({"rc": 0, "ended": "now"}), encoding="utf-8")
    adapter = RunRecordAdapter(tmp_path)
    assert adapter.job("run-1")["request"]["title"] == "do x"
    assert adapter.exit("run-1")["rc"] == 0


def test_run_record_rejects_traversal_even_if_file_exists(tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "job.json").write_text("{}", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    adapter = RunRecordAdapter(runs_dir)
    with pytest.raises(ValueError):
        adapter.job("../secret")
    with pytest.raises(ValueError):
        adapter.exit("../secret")


def test_run_record_missing_file_raises(tmp_path):
    adapter = RunRecordAdapter(tmp_path)
    with pytest.raises(FileNotFoundError):
        adapter.job("nope")


# --------------------------------------------------------------------------- #
# DecisionsAdapter (regex on the configured file)
# --------------------------------------------------------------------------- #

def test_decisions_regex(tmp_path):
    doc = tmp_path / "DECISIONS.md"
    doc.write_text(
        "# Decisions\n\n"
        "| id | decision |\n"
        "|----|----------|\n"
        "| D-209 | ship it |\n"
        "|D-13|missing spaces|\n"        # no spaces around the pipes: not the contract form
        "- D-14 not a table row\n",
        encoding="utf-8",
    )
    adapter = DecisionsAdapter(doc)
    assert adapter.has("D-209") is True
    assert adapter.has("d-209") is True
    assert adapter.has("D-13") is False  # only the pipe-spaced form counts
    assert adapter.has("D-14") is False
    assert adapter.has("D-999") is False
    assert adapter.has("") is False


def test_decisions_missing_file_is_empty(tmp_path):
    adapter = DecisionsAdapter(tmp_path / "nope.md")
    assert adapter.has("D-209") is False


def test_decisions_no_path_is_empty():
    assert DecisionsAdapter(None).has("D-209") is False


# --------------------------------------------------------------------------- #
# SemaphoreAdapter (token never leaks)
# --------------------------------------------------------------------------- #

def test_semaphore_real_fetcher_does_not_leak_token(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("SUPERSECRETTOKEN", encoding="utf-8")
    monkeypatch.setenv("PG_SEMAPHORE_URL", "https://semaphore.invalid")
    monkeypatch.setenv("PG_SEMAPHORE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("PG_SEMAPHORE_PROJECTS", "1")

    seen: dict = {}

    class Opener:
        def open(self, req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            raise urllib.error.URLError("boom SUPERSECRETTOKEN")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    adapter = SemaphoreAdapter()
    with pytest.raises(SemaphoreError) as excinfo:
        adapter.get(5, 1)
    assert "SUPERSECRETTOKEN" not in str(excinfo.value)
    assert "SUPERSECRETTOKEN" not in repr(adapter)
    assert seen["url"].startswith("https://semaphore.invalid")
    assert seen["url"] == "https://semaphore.invalid/api/project/1/tasks/5"
    assert seen["headers"].get("authorization") == "Bearer SUPERSECRETTOKEN"


def test_semaphore_missing_env_raises_sanitised(monkeypatch):
    monkeypatch.delenv("PG_SEMAPHORE_URL", raising=False)
    monkeypatch.delenv("PG_SEMAPHORE_TOKEN_FILE", raising=False)
    with pytest.raises(SemaphoreError):
        SemaphoreAdapter().get(5, 1)


def test_semaphore_missing_token_file_raises_sanitised(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_SEMAPHORE_URL", "https://semaphore.invalid")
    monkeypatch.setenv("PG_SEMAPHORE_TOKEN_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("PG_SEMAPHORE_PROJECTS", "1")
    with pytest.raises(SemaphoreError):
        SemaphoreAdapter().get(5, 1)


def test_semaphore_parses_json(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("t", encoding="utf-8")
    monkeypatch.setenv("PG_SEMAPHORE_URL", "https://semaphore.invalid/")
    monkeypatch.setenv("PG_SEMAPHORE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("PG_SEMAPHORE_PROJECTS", "1")

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=None):
            return b'{"status": "success"}'

    class Opener:
        def open(self, req, timeout=None):
            return Resp()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    assert SemaphoreAdapter().get(5, 1) == {"status": "success"}


# --------------------------------------------------------------------------- #
# GitAdapter, for real, against a temporary repository
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                    reason="git not available")
def test_git_adapter_real_repo(tmp_path):
    repo = tmp_path / "r"
    _git, _git_init = git_cli(tmp_path)
    _git_init(repo, "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Tester")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "do it\n\nPlan-Item: routing:T2-ONEPATH")
    _git(repo, "branch", "-M", "main")
    sha = _git(repo, "rev-parse", "HEAD")

    adapter = GitAdapter({"routing": str(repo)})
    assert adapter.repo_path("routing") == str(repo)
    assert adapter.repo_path("other") is None
    assert adapter.commit_exists(str(repo), sha) is True
    assert adapter.commit_exists(str(repo), "0" * 40) is False
    assert adapter.is_ancestor_of_main(str(repo), sha) is True
    assert adapter.trailers(str(repo), sha).get("Plan-Item") == "routing:T2-ONEPATH"
    assert adapter.commit_time(str(repo), sha) > 0


def test_parse_trailers_handles_blank_and_missing():
    text = "\nPlan-Item: routing:X\nCo-authored-by: A <a@b>\n\n"
    parsed = parse_trailers(text)
    assert parsed["Plan-Item"] == "routing:X"
    assert parsed["Co-authored-by"] == "A <a@b>"
    assert parse_trailers("") == {}


def test_semaphore_adapter_quotes_ids_and_rejects_non_integers(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("TOK", encoding="utf-8")
    monkeypatch.setenv("PG_SEMAPHORE_URL", "https://semaphore.invalid")
    monkeypatch.setenv("PG_SEMAPHORE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("PG_SEMAPHORE_PROJECTS", "1")
    seen = []

    class Opener:
        def open(self, req, timeout=None):
            seen.append(req.full_url)
            raise urllib.error.URLError("x")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    for bad in (("a/b", 1), (5, "../x"), (0, 1), (5, -2), (5, True)):
        with pytest.raises(SemaphoreError):
            SemaphoreAdapter().get(*bad)
    assert seen == []
    with pytest.raises(SemaphoreError):
        SemaphoreAdapter().get("5", "1")
    assert seen == ["https://semaphore.invalid/api/project/1/tasks/5"]


def _env(monkeypatch, tmp_path, projects):
    token_file = tmp_path / "token"
    token_file.write_text("TOK", encoding="utf-8")
    monkeypatch.setenv("PG_SEMAPHORE_URL", "https://semaphore.invalid")
    monkeypatch.setenv("PG_SEMAPHORE_TOKEN_FILE", str(token_file))
    if projects is None:
        monkeypatch.delenv("PG_SEMAPHORE_PROJECTS", raising=False)
    else:
        monkeypatch.setenv("PG_SEMAPHORE_PROJECTS", projects)
    seen = []

    class Opener:
        def open(self, req, timeout=None):
            seen.append(req.full_url)
            raise urllib.error.URLError("x")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    return seen


@pytest.mark.parametrize("value", [None, "", "  ", "abc", "1,x", "0", "1,,2", "-1", "9" * 5000])
def test_semaphore_allowlist_fails_closed(monkeypatch, tmp_path, value):
    seen = _env(monkeypatch, tmp_path, value)
    with pytest.raises(SemaphoreError, match="semaphore project allowlist not configured"):
        SemaphoreAdapter().get(5, 1)
    assert seen == []


def test_semaphore_project_not_in_allowlist(monkeypatch, tmp_path):
    seen = _env(monkeypatch, tmp_path, "1, 3")
    with pytest.raises(SemaphoreError, match="project not allowed"):
        SemaphoreAdapter().get(5, 2)
    assert seen == []
    with pytest.raises(SemaphoreError, match="transport|request failed"):
        SemaphoreAdapter().get(5, 3)
    assert seen == ["https://semaphore.invalid/api/project/3/tasks/5"]


def test_semaphore_reports_publicly_readable_projects(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, "1, 3")
    adapter = SemaphoreAdapter(fetch=lambda t, p: {})
    assert adapter.readable(1) is True
    assert adapter.readable("3") is True
    assert adapter.readable(2) is False
    assert adapter.readable("abc") is False
    assert adapter.readable(None) is False


@pytest.mark.parametrize("value", [None, "", "abc", "1,x"])
def test_semaphore_readable_fails_closed_without_allowlist(monkeypatch, tmp_path, value):
    _env(monkeypatch, tmp_path, value)
    assert SemaphoreAdapter(fetch=lambda t, p: {}).readable(1) is False


@pytest.mark.parametrize("code,msg", [
    (401, "semaphore auth refused (401)"), (403, "semaphore auth refused (403)"),
    (500, "semaphore request failed (HTTP 500)"), (404, "semaphore request failed (HTTP 404)"),
])
def test_semaphore_http_errors_distinct_and_token_free(monkeypatch, tmp_path, code, msg):
    _env(monkeypatch, tmp_path, "1")

    class Opener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "TOK reason", {"x": "TOK"}, None)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    with pytest.raises(SemaphoreError) as excinfo:
        SemaphoreAdapter().get(5, 1)
    assert str(excinfo.value) == msg
    assert "TOK" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# CIAdapter (gh api: check-runs, plus the workflow runs behind them)
# --------------------------------------------------------------------------- #

_SHA = "a" * 40


def _fake_gh(monkeypatch, checks, *, runs=None, runs_rc=0):
    """Serve ``gh api`` from fixtures: *checks* for the check-runs endpoint,
    *runs* for ``actions/runs`` (one payload, or a list of per-page payloads);
    *runs_rc* fails every workflow-runs call with that exit code.  ``runs=None``
    means the workflow-runs endpoint must never be asked.  Returns the list of
    requested API paths so a test can count and inspect the calls."""
    paths: list[str] = []

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["gh", "api"], cmd
        path = cmd[2]
        paths.append(path)
        if "/actions/runs" in path:
            if runs_rc:
                return subprocess.CompletedProcess(cmd, runs_rc, "", "gh failed")
            if runs is None:
                raise AssertionError(f"workflow-runs endpoint must not be called: {path}")
            page = int(path.rsplit("page=", 1)[1])
            payload = runs if isinstance(runs, dict) else runs[page - 1]
        else:
            payload = checks
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(evidence_ci.subprocess, "run", fake_run)
    return paths


def _check(app_slug, suite_id, *, name="build", check_id=1):
    """One check-run as GitHub returns it, for the app under *app_slug*."""
    return {
        "id": check_id, "name": name, "status": "completed", "conclusion": "success",
        "started_at": "2026-10-16T00:00:00Z", "head_sha": _SHA,
        "app": {"slug": app_slug}, "check_suite": {"id": suite_id},
        "details_url": f"https://example.invalid/{check_id}",
    }


def test_ci_adapter_backfills_workflow_for_dispatch_and_pr_suites(monkeypatch):
    checks = {
        "total_count": 3,
        "check_runs": [
            _check("github-actions", 111, name="build"),
            _check("github-actions", 222, name="test", check_id=2),
            _check("circleci", 333, name="lint", check_id=3),  # not an actions suite
        ],
    }
    runs = {
        "total_count": 2,
        "workflow_runs": [
            {"id": 9001, "check_suite_id": 111, "event": "push", "name": "CI",
             "workflow_id": 5, "run_attempt": 1,
             "status": "completed", "conclusion": "success"},
            {"id": 9002, "check_suite_id": 222, "event": "pull_request", "name": "Tests",
             "workflow_id": 7, "run_attempt": 1,
             "status": "completed", "conclusion": "success"},
        ],
    }
    paths = _fake_gh(monkeypatch, checks, runs=runs)
    out = CIAdapter().check_runs("owner/repo", _SHA)
    assert out[0]["workflow"] == {
        "event": "push", "workflow_id": 5, "name": "CI", "run_id": 9001,
    }
    assert out[1]["workflow"] == {
        "event": "pull_request", "workflow_id": 7, "name": "Tests", "run_id": 9002,
    }
    # The sha-scoped workflow-runs endpoint was used for the second call only.
    assert len(paths) == 2
    assert paths[1].startswith(
        f"repos/owner/repo/actions/runs?head_sha={_SHA}&per_page=100&page=1"
    )
    # Existing keys are untouched alongside the added one.
    assert out[0]["name"] == "build"
    assert out[0]["check_suite"] == {"id": 111}


def test_ci_adapter_unmatched_suite_gets_no_workflow_key(monkeypatch):
    checks = {"total_count": 2, "check_runs": [
        _check("github-actions", 777, name="build"),
        _check("github-actions", 111, name="test", check_id=2),  # suite matches below
    ]}
    runs = {
        "total_count": 1,
        "workflow_runs": [
            {"id": 42, "check_suite_id": 111, "event": "push", "name": "CI",
             "workflow_id": 5, "run_attempt": 1,
             "status": "completed", "conclusion": "success"},
        ],
    }
    _fake_gh(monkeypatch, checks, runs=runs)
    out = CIAdapter().check_runs("owner/repo", _SHA)
    assert len(out) == 2
    assert "workflow" not in out[0]  # no workflow run shares suite 777
    assert out[1]["workflow"] == {"event": "push", "workflow_id": 5,
                                  "name": "CI", "run_id": 42}
    assert out[0]["id"] == 1  # everything else stays as collected


def test_ci_adapter_null_check_suite_ids_never_backfill(monkeypatch):
    # A null check-suite id on either side may never match: the run keyed under
    # None used to backfill the check-run whose own check_suite.id is None.
    checks = {"total_count": 2, "check_runs": [
        _check("github-actions", None, name="build"),
        _check("github-actions", 111, name="test", check_id=2),
    ]}
    runs = {
        "total_count": 2,
        "workflow_runs": [
            {"id": 9001, "check_suite_id": None, "event": "push", "name": "CI",
             "workflow_id": 5, "run_attempt": 1,
             "status": "completed", "conclusion": "success"},
            {"id": 9002, "check_suite_id": 111, "event": "push", "name": "CI",
             "workflow_id": 5, "run_attempt": 1,
             "status": "completed", "conclusion": "success"},
        ],
    }
    _fake_gh(monkeypatch, checks, runs=runs)
    out = CIAdapter().check_runs("owner/repo", _SHA)
    assert len(out) == 2
    assert "workflow" not in out[0]  # no key is ever invented for a null suite
    assert out[1]["workflow"] == {"event": "push", "workflow_id": 5,
                                  "name": "CI", "run_id": 9002}


def test_ci_adapter_bool_check_suite_id_never_backfills(monkeypatch):
    # True == 1 in Python: without the int-only gate a bool suite id would
    # hijack suite 1 and invent a 'workflow' key on either side of the match.
    run = {"id": 42, "check_suite_id": True, "event": "push", "name": "CI",
           "workflow_id": 5, "run_attempt": 1,
           "status": "completed", "conclusion": "success"}
    checks = {"total_count": 1, "check_runs": [_check("github-actions", 1)]}
    _fake_gh(monkeypatch, checks, runs={"total_count": 1, "workflow_runs": [run]})
    out = CIAdapter().check_runs("owner/repo", _SHA)
    assert "workflow" not in out[0]  # a bool workflow-run suite id matches nothing

    checks = {"total_count": 1, "check_runs": [_check("github-actions", True)]}
    run = dict(run, check_suite_id=1)
    _fake_gh(monkeypatch, checks, runs={"total_count": 1, "workflow_runs": [run]})
    out = CIAdapter().check_runs("owner/repo", _SHA)
    assert "workflow" not in out[0]  # a bool check-run suite id is no key either


def test_ci_adapter_workflow_run_without_workflow_id_gets_no_key(monkeypatch):
    # The suite matches, but the run has no workflow_id to group by: the
    # check-run keeps its plain suite key instead of a half-empty 'workflow'.
    checks = {"total_count": 1, "check_runs": [_check("github-actions", 111)]}
    runs = {
        "total_count": 1,
        "workflow_runs": [
            {"id": 42, "check_suite_id": 111, "event": "workflow_dispatch",
             "name": "CI", "run_attempt": 1,
             "status": "completed", "conclusion": "success"},
        ],
    }
    _fake_gh(monkeypatch, checks, runs=runs)
    out = CIAdapter().check_runs("owner/repo", _SHA)
    assert len(out) == 1
    assert "workflow" not in out[0]


@pytest.mark.parametrize("bad", [
    {"id": None}, {"id": True}, {"id": 0}, {"id": "42"},
    {"workflow_id": None}, {"workflow_id": True}, {"workflow_id": 0},
    {"event": None}, {"event": 7},
])
def test_ci_adapter_incomplete_workflow_run_gets_no_key(monkeypatch, bad):
    # 'workflow' is written only from a positive int id and workflow_id
    # (bool excluded) and a str event; anything else adds no key at all.
    run = {"id": 42, "check_suite_id": 111, "event": "push", "name": "CI",
           "workflow_id": 5, "run_attempt": 1,
           "status": "completed", "conclusion": "success"}
    run.update(bad)
    checks = {"total_count": 1, "check_runs": [_check("github-actions", 111)]}
    _fake_gh(monkeypatch, checks, runs={"total_count": 1, "workflow_runs": [run]})
    out = CIAdapter().check_runs("owner/repo", _SHA)
    assert "workflow" not in out[0]


def test_ci_adapter_without_actions_runs_makes_exactly_one_gh_call(monkeypatch):
    checks = {"total_count": 2, "check_runs": [
        _check("circleci", 111, name="lint"),
        _check("codecov", 222, name="coverage", check_id=2),
    ]}
    paths = _fake_gh(monkeypatch, checks)  # runs=None: second call must not happen
    out = CIAdapter().check_runs("owner/repo", _SHA)
    assert len(out) == 2
    assert len(paths) == 1
    assert "/check-runs" in paths[0]


def test_ci_adapter_workflow_runs_truncated_is_refused(monkeypatch):
    checks = {"total_count": 1, "check_runs": [_check("github-actions", 111)]}
    runs = [
        {"total_count": 250,
         "workflow_runs": [{"id": i, "check_suite_id": 111, "event": "push",
                            "name": "CI", "workflow_id": 5, "run_attempt": 1,
                            "status": "completed", "conclusion": "success"}
                           for i in range(1, 101)]},
        {"total_count": 250, "workflow_runs": []},
    ]
    paths = _fake_gh(monkeypatch, checks, runs=runs)
    with pytest.raises(RuntimeError, match="workflow-runs truncated"):
        CIAdapter().check_runs("owner/repo", _SHA)
    assert any(p.endswith("page=2") and "/actions/runs" in p for p in paths)


def test_ci_adapter_gh_failure_on_second_call_raises(monkeypatch):
    checks = {"total_count": 1, "check_runs": [_check("github-actions", 111)]}
    paths = _fake_gh(monkeypatch, checks, runs_rc=2)
    with pytest.raises(RuntimeError, match="gh api exited 2"):
        CIAdapter().check_runs("owner/repo", _SHA)
    assert len(paths) == 2  # the failure came from the workflow-runs call


@pytest.mark.skipif(shutil.which("git") is None or not git_supports_init_branch(),
                    reason="git >= 2.28 not available (git init -b)")
def test_git_adapter_merge_commit_of_real_repo(tmp_path: Path) -> None:
    repo = tmp_path
    _git, _git_init = git_cli(tmp_path)
    _git_init(repo, "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")

    _git(repo, "commit", "--allow-empty", "-m", "c0")
    c0 = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "-c", "feat")
    _git(repo, "commit", "--allow-empty", "-m", "f1")
    f1 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "--allow-empty", "-m", "f2")
    f2 = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "main")
    _git(repo, "config", "user.email", "merger@example.invalid")
    _git(repo, "merge", "--no-ff", "-m", "Merge feat", "feat")
    M1 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "config", "user.email", "test@example.invalid")

    _git(repo, "commit", "--allow-empty", "-m", "d1")
    d1 = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "-c", "feat2", d1)
    _git(repo, "commit", "--allow-empty", "-m", "g1")
    g1 = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "main")
    _git(repo, "config", "user.email", "merger@example.invalid")
    _git(repo, "merge", "--no-ff", "-m", "Merge feat2", "feat2")
    M2 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "config", "user.email", "test@example.invalid")

    _git(repo, "switch", "-c", "wip", "main")
    _git(repo, "commit", "--allow-empty", "-m", "w1")
    w1 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")

    adapter = GitAdapter()
    email = "merger@example.invalid"

    assert adapter.merge_commit_of(repo, f1, "main") == (M1, "merger@example.invalid")
    assert adapter.merge_commit_of(repo, f2, "main") == (M1, "merger@example.invalid")
    assert adapter.merge_commit_of(repo, M1, "main") == (M1, email)
    assert adapter.merge_commit_of(repo, d1, "main") is None
    assert adapter.merge_commit_of(repo, c0, "main") is None
    assert adapter.merge_commit_of(repo, w1, "main") is None
    assert adapter.merge_commit_of(repo, g1, "main") == (M2, email)
    assert adapter.merge_commit_of(repo, "abc", "main") is None
    assert adapter.merge_commit_of(repo, f1, "-x") is None
    assert adapter.head_sha(repo, "main") == M2
    assert adapter.head_sha(repo, "nope") is None
    assert adapter.head_sha(repo, "-x") is None


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("x"),
        ValueError("y"),
        SemaphoreError("z"),
    ],
)
def test_semaphore_readable_fails_closed_on_unexpected_exception(monkeypatch, exc):
    from plangraph.keeper import evidence_semaphore as sem_mod

    def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(sem_mod, "_allowed_projects", _raise)
    assert SemaphoreAdapter(fetch=lambda t, p: {}).readable(1) is False
