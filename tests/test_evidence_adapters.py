"""Tests for the real evidence adapters (local git only, no network)."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

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

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                    reason="git not available")
def test_git_adapter_real_repo(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
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


@pytest.mark.parametrize("value", [None, "", "  ", "abc", "1,x", "0", "1,,2", "-1"])
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
