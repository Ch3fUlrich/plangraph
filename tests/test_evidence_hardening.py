"""Security-hardening tests for the evidence verifiers.

Each block maps to one finding of the evidence review. These tests fail before
the corresponding fix and are written without touching git, the network or any
path outside ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from types import SimpleNamespace

import pytest

from evidence_fakes import CLAIM, ITEM, SHA, FakeCI, FakeGit, FakeRuns, make_ctx, verify
from plangraph.keeper import evidence as ev
from plangraph.keeper import evidence_ci, evidence_git, evidence_semaphore
from plangraph.keeper.evidence_ci import CIAdapter
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.evidence_git import GitAdapter
from plangraph.keeper.evidence_runs import RunRecordAdapter
from plangraph.keeper.evidence_semaphore import SemaphoreAdapter, SemaphoreError


class _Boom:
    """Every attribute access raises: a verifier must never let it escape."""

    def __getattr__(self, name):
        raise OSError("SECRET-TOKEN-in-args")


# --------------------------------------------------------------------------- #
# (1) commit ref must be a full lowercase hex sha, checked before any I/O
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", [
    "main", "HEAD", "abc1234", "A" * 40, "g" * 40, "a" * 39, "a" * 41,
    "-h", "--upload-pack=evil", "a" * 40 + " ", "deadbeef",
])
def test_commit_ci_rejects_non_full_sha_before_any_git(bad):
    git = FakeGit()
    res = verify("commit+ci", {"commit": bad}, make_ctx(git=git))
    assert res.ok is False
    assert git.repo_path_calls == []  # validation precedes every git/gh call


def test_commit_ci_accepts_full_64_hex_sha():
    sha = "c" * 64
    ctx = make_ctx(ci=FakeCI(runs=[{"name": "b", "head_sha": sha, "conclusion": "success"}]))
    assert verify("commit+ci", {"commit": sha}, ctx).ok is True


def test_git_adapter_rejects_option_like_shas_without_calling_git(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(evidence_git.subprocess, "run", lambda *a, **k: calls.append(a))
    adapter = GitAdapter({"p": str(tmp_path)})
    assert adapter.commit_exists(str(tmp_path), "-h") is False
    assert adapter.is_ancestor_of_main(str(tmp_path), "--upload-pack=evil") is False
    assert adapter.trailers(str(tmp_path), "main") == {}
    assert adapter.commit_time(str(tmp_path), "abc1234") is None
    assert calls == []


# --------------------------------------------------------------------------- #
# (2) validated shas are positional after --end-of-options
# --------------------------------------------------------------------------- #

def test_git_adapter_puts_validated_sha_after_end_of_options(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(evidence_git.subprocess, "run", fake_run)
    adapter = GitAdapter({"p": str(tmp_path)})
    sha = "a" * 40
    adapter.commit_exists(str(tmp_path), sha)
    assert "--end-of-options" in seen["cmd"]
    sha_at = next(i for i, arg in enumerate(seen["cmd"]) if arg.startswith(sha))
    assert sha_at > seen["cmd"].index("--end-of-options")


# --------------------------------------------------------------------------- #
# (3) run-record symlink escape
# --------------------------------------------------------------------------- #

def test_run_record_rejects_symlinked_run_dir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "job.json").write_text("{}", encoding="utf-8")
    (outside / "exit.json").write_text("{}", encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    os.symlink(outside, runs / "bad")
    adapter = RunRecordAdapter(runs)
    with pytest.raises(ValueError):
        adapter.job("bad")
    with pytest.raises(ValueError):
        adapter.exit("bad")


def test_run_record_rejects_symlinked_job_and_exit(tmp_path):
    run = tmp_path / "runs" / "run-1"
    run.mkdir(parents=True)
    secret = tmp_path / "secret.json"
    secret.write_text('{"rc": 0}', encoding="utf-8")
    os.symlink(secret, run / "job.json")
    os.symlink(secret, run / "exit.json")
    adapter = RunRecordAdapter(tmp_path / "runs")
    with pytest.raises(ValueError):
        adapter.job("run-1")
    with pytest.raises(ValueError):
        adapter.exit("run-1")


def test_run_record_verifier_fails_closed_on_symlinked_run_dir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "job.json").write_text(
        json.dumps({"request": {"title": ITEM["key"]}, "task": ""}), encoding="utf-8")
    (outside / "exit.json").write_text('{"rc": 0}', encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    os.symlink(outside, runs / "run-1")
    res = verify("run-record", {"run_id": "run-1"}, make_ctx(runs=RunRecordAdapter(runs)))
    assert res.ok is False


# --------------------------------------------------------------------------- #
# (4) both author and committer dates, with a configurable skew
# --------------------------------------------------------------------------- #

def test_commit_ci_author_older_than_claim_fails():
    ctx = make_ctx(git=FakeGit(commit_time=CLAIM + 100, author_time=CLAIM - 1000))
    assert verify("commit+ci", {"commit": SHA}, ctx).ok is False


def test_commit_ci_committer_older_than_claim_fails():
    ctx = make_ctx(git=FakeGit(commit_time=CLAIM - 1000, author_time=CLAIM + 100))
    assert verify("commit+ci", {"commit": SHA}, ctx).ok is False


def test_commit_ci_within_default_skew_passes():
    ctx = make_ctx(git=FakeGit(commit_time=CLAIM - 30))
    assert verify("commit+ci", {"commit": SHA}, ctx).ok is True


def test_commit_ci_skew_is_configurable():
    ctx = make_ctx(git=FakeGit(commit_time=CLAIM - 1))
    ctx.skew = 0.0
    assert verify("commit+ci", {"commit": SHA}, ctx).ok is False


# --------------------------------------------------------------------------- #
# (5) gh api path built only from a validated slug and sha
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_slug", [
    "owner/repo?x=1", "owner/repo/extra", "owner/../etc",
    "owner/rep o", "own er/repo", "owner/repo#frag",
])
def test_ci_adapter_rejects_bad_slug_before_gh(monkeypatch, bad_slug):
    calls: list = []
    monkeypatch.setattr(evidence_ci.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(Exception):
        CIAdapter().check_runs(bad_slug, SHA)
    assert calls == []


@pytest.mark.parametrize("bad_sha", ["main", "-h", "abc1234", "a" * 39])
def test_ci_adapter_rejects_bad_sha_before_gh(monkeypatch, bad_sha):
    calls: list = []
    monkeypatch.setattr(evidence_ci.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(Exception):
        CIAdapter().check_runs("owner/repo", bad_sha)
    assert calls == []


def test_ci_adapter_builds_path_from_validated_components(monkeypatch):
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"check_runs": []}), "")

    monkeypatch.setattr(evidence_ci.subprocess, "run", fake_run)
    CIAdapter().check_runs("My-Org/repo.name", SHA)
    assert seen["cmd"][:2] == ["gh", "api"]
    assert seen["cmd"][2].startswith(f"repos/My-Org/repo.name/commits/{SHA}/check-runs")
    assert "per_page=100" in seen["cmd"][2]
    assert "page=1" in seen["cmd"][2]


# --------------------------------------------------------------------------- #
# (6) decisions: code blocks never count as recorded decisions
# --------------------------------------------------------------------------- #

def test_decisions_ignores_fenced_and_indented_code(tmp_path):
    doc = tmp_path / "DECISIONS.md"
    doc.write_text(
        "# Decisions\n\n"
        "```\n"
        "| D-1 | fake in a fence |\n"
        "```\n\n"
        "~~~\n"
        "| D-2 | fake in a tilde fence |\n"
        "~~~\n\n"
        "    | D-3 | fake, indented code |\n\n"
        "| D-4 | the only real row |\n",
        encoding="utf-8",
    )
    adapter = DecisionsAdapter(doc)
    assert adapter.has("D-1") is False
    assert adapter.has("D-2") is False
    assert adapter.has("D-3") is False
    assert adapter.has("D-4") is True


# --------------------------------------------------------------------------- #
# (7) semaphore: refuse redirects, reject unsafe base URLs, cap the body
# --------------------------------------------------------------------------- #

def test_semaphore_refuses_redirects_without_leaking(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("SUPERSECRETTOKEN", encoding="utf-8")
    monkeypatch.setenv("PG_SEMAPHORE_URL", "https://semaphore.invalid")
    monkeypatch.setenv("PG_SEMAPHORE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("PG_SEMAPHORE_PROJECTS", "1")

    handler = evidence_semaphore._NoRedirect()
    with pytest.raises(SemaphoreError) as excinfo:
        handler.redirect_request(
            None, None, 302, "Found", {}, "https://evil.invalid/?q=SUPERSECRETTOKEN")
    assert "SUPERSECRETTOKEN" not in str(excinfo.value)
    assert "evil.invalid" not in str(excinfo.value)


def test_semaphore_refuses_redirect_end_to_end(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("SUPERSECRETTOKEN", encoding="utf-8")
    monkeypatch.setenv("PG_SEMAPHORE_URL", "https://semaphore.invalid")
    monkeypatch.setenv("PG_SEMAPHORE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("PG_SEMAPHORE_PROJECTS", "1")

    class Resp:
        status = 302

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=None):
            return b""

    class Opener:
        def open(self, req, timeout=None):
            # What urllib does on a redirect before replaying the request.
            return evidence_semaphore._NoRedirect().redirect_request(
                req, Resp(), 302, "Found", {}, "https://evil.invalid")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    with pytest.raises(SemaphoreError):
        SemaphoreAdapter().get(5, 1)


@pytest.mark.parametrize("bad_base", [
    "ftp://semaphore.invalid", "file:///etc/passwd",
    "https://user:pass@semaphore.invalid", "//semaphore.invalid",
])
def test_semaphore_rejects_unsafe_base_urls(monkeypatch, tmp_path, bad_base):
    token_file = tmp_path / "token"
    token_file.write_text("t", encoding="utf-8")
    monkeypatch.setenv("PG_SEMAPHORE_URL", bad_base)
    monkeypatch.setenv("PG_SEMAPHORE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("PG_SEMAPHORE_PROJECTS", "1")
    with pytest.raises(SemaphoreError):
        SemaphoreAdapter().get(5, 1)


def test_semaphore_caps_response_size(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("t", encoding="utf-8")
    monkeypatch.setenv("PG_SEMAPHORE_URL", "https://semaphore.invalid")
    monkeypatch.setenv("PG_SEMAPHORE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("PG_SEMAPHORE_PROJECTS", "1")

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=None):
            return b"x" * (2 * 1024 * 1024)

    class Opener:
        def open(self, req, timeout=None):
            return Resp()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    with pytest.raises(SemaphoreError):
        SemaphoreAdapter().get(5, 1)


# --------------------------------------------------------------------------- #
# (8) a verifier never raises and never passes on an adapter failure
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kind,ref", [
    ("commit+ci", {"commit": SHA}),
    ("run-record", {"run_id": "run-1"}),
    ("semaphore-task", {"task_id": 5, "project_id": 1, "template": 7}),
    ("decision", {"id": "D-209"}),
])
def test_verifier_never_raises_when_an_adapter_breaks(kind, ref):
    ctx = SimpleNamespace(git=_Boom(), ci=_Boom(), runs=_Boom(),
                          semaphore=_Boom(), decisions=_Boom(),
                          now=0.0, claim_time=CLAIM)
    res = ev.VERIFIERS[kind].verify(ITEM, ref, ctx)
    assert res.ok is False
    assert "SECRET" not in res.detail


def test_verifier_guard_catches_unexpected_exception():
    res = ev.VERIFIERS["commit+ci"].verify(None, {"commit": SHA}, make_ctx())
    assert res.ok is False
    assert res.detail == "AttributeError"


def test_commit_ci_subprocess_timeout_is_sanitised():
    ctx = make_ctx(git=FakeGit(commit_time_error=subprocess.TimeoutExpired("git", 30)))
    res = verify("commit+ci", {"commit": SHA}, ctx)
    assert res.ok is False
    assert "TimeoutExpired" in res.detail
    assert "30" not in res.detail


def test_run_record_json_error_is_sanitised():
    ctx = make_ctx(runs=FakeRuns(job_error=ValueError("bad json at /etc/secret")))
    res = verify("run-record", {"run_id": "run-1"}, ctx)
    assert res.ok is False
    assert "/etc/secret" not in res.detail


def test_dispatch_never_raises_on_unhashable_kind():
    res = ev.verify([], ITEM, {}, make_ctx())
    assert res.ok is False
    assert res.detail == "TypeError"


def test_semaphore_opener_ignores_proxy_env(monkeypatch):
    # The bearer must never be sent to a proxy named by the environment.
    import urllib.request
    from plangraph.keeper.evidence_semaphore import SemaphoreAdapter
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    opener = SemaphoreAdapter._opener()
    proxies = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    # An empty ProxyHandler registers no *_open method, so none stays installed;
    # the default env-reading one must not be there either.
    assert all(h.proxies == {} for h in proxies)
    default = urllib.request.build_opener()
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies
               for h in default.handlers)  # the env really is read by default


def test_ci_adapter_paginates_two_pages(monkeypatch):
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        path = cmd[2]
        if path.endswith("page=1"):
            runs = [{"id": i, "name": f"c{i}"} for i in range(1, 101)]
            payload = {"check_runs": runs, "total_count": 105}
        elif path.endswith("page=2"):
            runs = [{"id": i, "name": f"c{i}"} for i in range(101, 106)]
            payload = {"check_runs": runs, "total_count": 105}
        else:
            raise AssertionError(f"unexpected page in {path!r}")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(evidence_ci.subprocess, "run", fake_run)
    runs = CIAdapter().check_runs("owner/repo", SHA)
    assert len(runs) == 105
    assert any("per_page=100" in c[2] and c[2].endswith("page=2") for c in calls)
    assert any("per_page=100" in c[2] and c[2].endswith("page=1") for c in calls)


def test_ci_adapter_truncated_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        path = cmd[2]
        if path.endswith("page=1"):
            runs = [{"id": i, "name": f"c{i}"} for i in range(1, 101)]
            payload = {"check_runs": runs, "total_count": 250}
        else:
            payload = {"check_runs": [], "total_count": 250}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(evidence_ci.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="check-runs truncated"):
        CIAdapter().check_runs("owner/repo", SHA)
