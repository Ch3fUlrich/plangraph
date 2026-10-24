"""Tests for the GitHub REST adapter."""
from __future__ import annotations

import base64
import json

import pytest

from plangraph.planner.github_adapter import RestGitHub
from plangraph.planner.phase_finish import GitHubError

TOKEN = "tok-" + "sentinel"

def test_merge_pr_sends_exact_commit_message(tmp_path):
    gh, opener = make_github(tmp_path, [(200, {"merged": True, "sha": "b" * 40})])
    message = "Merge phase demo:PH-X\n\nPlan-Item: demo:C1\nPlan-Item: demo:C2\n"
    gh.merge_pr(7, "a" * 40, commit_message=message)
    assert opener.calls[0][0] == "PUT"
    assert json.loads(opener.calls[0][3]) == {
        "sha": "a" * 40, "merge_method": "merge",
        "commit_title": "Merge phase demo:PH-X", "commit_message": message,
    }

class FakeOpener:
    def __init__(self, replies: list[tuple[int, object]]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, object]:
        self.calls.append((method, url, dict(headers), body))
        return self.replies.pop(0)

class FakeGitRunner:
    def __init__(self, code: int = 0, stderr: str = "") -> None:
        self.code = code
        self.stderr = stderr
        self.calls: list[tuple[list[str], str, dict[str, str]]] = []

    def __call__(
        self, args: list[str], cwd: str, env: dict[str, str]
    ) -> tuple[int, str]:
        self.calls.append((list(args), cwd, dict(env)))
        return self.code, self.stderr

def token_file(tmp_path):
    path = tmp_path / "token.txt"
    path.write_text(TOKEN, encoding="utf-8")
    return path

def make_github(tmp_path, replies, *, git_runner=None):
    opener = FakeOpener(replies)
    gh = RestGitHub(
        "owner/repo",
        token_file(tmp_path),
        opener=opener,
        git_runner=git_runner,
    )
    return gh, opener

def test_find_pr_empty_returns_none(tmp_path):
    gh, opener = make_github(tmp_path, [(200, [])])
    assert gh.find_pr("phase-1") is None
    method, url, headers, body = opener.calls[0]
    assert method == "GET"
    expected = (
        "https://api.github.com/repos/owner/repo/pulls"
        "?head=owner%3Aphase-1&state=all&per_page=1"
    )
    assert url == expected
    assert headers["Authorization"] == "Bearer " + TOKEN
    assert body is None

def test_find_pr_merged_at_maps_merged(tmp_path):
    head = "a" * 40
    item = {
        "number": 7,
        "html_url": "https://github.com/owner/repo/pull/7",
        "state": "closed",
        "merged_at": "2024-01-01T00:00:00Z",
        "head": {"sha": head},
    }
    gh, _ = make_github(tmp_path, [(200, [item])])
    assert gh.find_pr("phase") == {
        "number": 7,
        "url": "https://github.com/owner/repo/pull/7",
        "state": "merged",
        "head": head,
    }

def test_open_pr_request_body(tmp_path):
    head = "b" * 40
    item = {
        "number": 8,
        "html_url": "https://github.com/owner/repo/pull/8",
        "state": "open",
        "head": {"sha": head},
    }
    gh, opener = make_github(tmp_path, [(201, item)])
    assert gh.open_pr("phase", "main", "Title", "Body") == {
        "number": 8,
        "url": "https://github.com/owner/repo/pull/8",
        "state": "open",
        "head": head,
    }
    method, url, headers, body = opener.calls[0]
    assert method == "POST"
    assert url == "https://api.github.com/repos/owner/repo/pulls"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {
        "title": "Title",
        "head": "phase",
        "base": "main",
        "body": "Body",
    }

def test_ci_green_only_ci_gate_completed_success(tmp_path):
    head = "c" * 40
    replies = [
        (200, {"check_runs": [
            {"id": 1, "name": "ci-gate", "status": "completed", "conclusion": "success", "head_sha": head},
        ]}),
        (200, {"check_runs": [
            {"id": 2, "name": "other", "status": "completed", "conclusion": "success", "head_sha": head},
        ]}),
        (200, {"check_runs": [
            {"id": 3, "name": "ci-gate", "status": "in_progress", "conclusion": None, "head_sha": head},
        ]}),
        (200, {"check_runs": [
            {"id": 4, "name": "ci-gate", "status": "completed", "conclusion": "failure", "head_sha": head},
        ]}),
    ]
    gh, opener = make_github(tmp_path, replies)
    assert gh.ci_green(head) is True
    assert gh.ci_green(head) is False
    assert gh.ci_green(head) is False
    assert gh.ci_green(head) is False
    expected = (
        f"https://api.github.com/repos/owner/repo/commits/{head}"
        "/check-runs?per_page=100"
    )
    assert opener.calls[0][1] == expected

def test_merge_pr_sends_sha_and_maps_conflict(tmp_path):
    head = "d" * 40
    merge_sha = "a" * 40
    gh, opener = make_github(tmp_path, [(200, {"merged": True, "sha": merge_sha})])
    assert gh.merge_pr(9, head) == {
        "number": 9,
        "url": "https://github.com/owner/repo/pull/9",
        "state": "merged",
        "head": head,
        "merge_sha": merge_sha,
    }
    method, url, _headers, body = opener.calls[0]
    assert method == "PUT"
    assert url == "https://api.github.com/repos/owner/repo/pulls/9/merge"
    assert json.loads(body) == {"sha": head, "merge_method": "merge"}
    conflict, _ = make_github(tmp_path, [(409, {})])
    with pytest.raises(GitHubError) as exc:
        conflict.merge_pr(9, head)
    assert "409" in str(exc.value)
    assert TOKEN not in str(exc.value)

def test_push_branch_argv_env_and_no_token_leak(tmp_path):
    head = "e" * 40
    runner = FakeGitRunner(code=0)
    gh, _ = make_github(tmp_path, [], git_runner=runner)
    gh.push_branch(tmp_path, "phase-2", head)
    args, cwd, env = runner.calls[0]
    expected_args = [
        "git",
        "push",
        "https://github.com/owner/repo.git",
        f"{head}:refs/heads/phase-2",
    ]
    assert args == expected_args
    assert cwd == str(tmp_path)
    expected_basic = base64.b64encode(
        f"x-access-token:{TOKEN}".encode("utf-8")
    ).decode("ascii")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Basic " + expected_basic
    for arg in args:
        assert TOKEN not in arg

    leaky = FakeGitRunner(
        code=1,
        stderr=f"fatal https://x-access-token:{TOKEN}@github.com/owner/repo",
    )
    failing, _ = make_github(tmp_path, [], git_runner=leaky)
    with pytest.raises(GitHubError) as exc:
        failing.push_branch(tmp_path, "phase-2", head)
    assert str(exc.value) == "git push failed"
    assert TOKEN not in str(exc.value)

def test_missing_or_empty_token_file_does_not_leak_path(tmp_path):
    missing = tmp_path / "do-not-echo-this-name"
    gh = RestGitHub("owner/repo", missing, opener=FakeOpener([]))
    with pytest.raises(GitHubError) as exc:
        gh.find_pr("phase")
    msg = str(exc.value)
    assert msg == "github token file unavailable"
    assert str(missing) not in msg
    assert missing.name not in msg

    empty = tmp_path / "empty-token"
    empty.write_text("  \n", encoding="utf-8")
    gh2 = RestGitHub("owner/repo", empty, opener=FakeOpener([]))
    with pytest.raises(GitHubError) as exc2:
        gh2.find_pr("phase")
    msg2 = str(exc2.value)
    assert msg2 == "github token file unavailable"
    assert str(empty) not in msg2
    assert empty.name not in msg2

def test_from_env_none_cases_and_success(tmp_path):
    token_path = token_file(tmp_path)
    assert RestGitHub.from_env({}) is None
    assert RestGitHub.from_env({"PG_PHASE_GITHUB_REPO": "owner/repo"}) is None
    assert RestGitHub.from_env(
        {"PG_PHASE_GITHUB_TOKEN_FILE": str(token_path)}
    ) is None
    assert RestGitHub.from_env(
        {"PG_PHASE_GITHUB_REPO": "", "PG_PHASE_GITHUB_TOKEN_FILE": str(token_path)}
    ) is None
    gh = RestGitHub.from_env(
        {
            "PG_PHASE_GITHUB_REPO": "owner/repo",
            "PG_PHASE_GITHUB_TOKEN_FILE": str(token_path),
        }
    )
    assert isinstance(gh, RestGitHub)
    assert gh.repo == "owner/repo"
    assert gh._token() == TOKEN

def test_bad_repo_name_raises_value_error(tmp_path):
    path = token_file(tmp_path)
    for repo in ["owner", "owner/repo/extra", "owner name/repo", "/repo"]:
        with pytest.raises(ValueError):
            RestGitHub(repo, path)
import os

import pytest

@pytest.fixture(autouse=True)
def _private_token_umask():
    # Existing token_file() writes must be private under the new mode check.
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)

def test_ci_green_second_page_success(tmp_path):
    head = "f" * 40
    page1 = {
        "total_count": 101,
        "check_runs": [
            {
                "id": i,
                "name": "other",
                "head_sha": head,
                "status": "completed",
                "conclusion": "success",
            }
            for i in range(1, 101)
        ],
    }
    page2 = {
        "total_count": 101,
        "check_runs": [
            {
                "id": 101,
                "name": "ci-gate",
                "head_sha": head,
                "status": "completed",
                "conclusion": "success",
            }
        ],
    }
    gh, opener = make_github(tmp_path, [(200, page1), (200, page2)])
    assert gh.ci_green(head) is True
    assert len(opener.calls) == 2
    assert "page=2" in opener.calls[1][1]

def test_ci_green_wrong_head_sha_ignored(tmp_path):
    head = "a" * 40
    wrong = "b" * 40
    gh, _ = make_github(
        tmp_path,
        [
            (
                200,
                {
                    "check_runs": [
                        {
                            "id": 1,
                            "name": "ci-gate",
                            "head_sha": wrong,
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                },
            )
        ],
    )
    assert gh.ci_green(head) is False

def test_find_pr_malformed_reply_raises_github_error(tmp_path):
    item = {"number": 1, "html_url": "url", "state": "open"}
    gh, _ = make_github(tmp_path, [(200, [item])])
    with pytest.raises(GitHubError) as exc:
        gh.find_pr("phase")
    assert str(exc.value) == "github malformed reply"

def test_merge_pr_not_confirmed_raises_github_error(tmp_path):
    head = "d" * 40
    gh, _ = make_github(tmp_path, [(200, {"merged": False})])
    with pytest.raises(GitHubError) as exc:
        gh.merge_pr(9, head)
    assert str(exc.value) == "github merge not confirmed"

def test_push_branch_env_allowlist_excludes_sentinel(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_TEST_SENTINEL", "secret")
    head = "e" * 40
    runner = FakeGitRunner(code=0)
    gh, _ = make_github(tmp_path, [], git_runner=runner)
    gh.push_branch(tmp_path, "phase-2", head)
    _args, _cwd, env = runner.calls[0]
    assert "PG_TEST_SENTINEL" not in env

def test_token_file_group_or_other_bits_refused(tmp_path):
    path = tmp_path / "token.txt"
    path.write_text(TOKEN, encoding="utf-8")
    path.chmod(0o644)
    gh = RestGitHub("owner/repo", path, opener=FakeOpener([]))
    with pytest.raises(GitHubError) as exc:
        gh.find_pr("phase")
    assert str(exc.value) == "github token file unavailable"
