"""GitHub REST adapter for the phase-finish keeper.

The keeper uses its own fine-grained token from a file. Tokens never enter
argv, logs, or exception text.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from plangraph.planner.phase_finish import GitHubError

__all__ = ["RestGitHub"]

_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")

Opener = Callable[[str, str, dict[str, str], bytes | None], tuple[int, Any]]
GitRunner = Callable[[list[str], str, dict[str, str]], tuple[int, str]]

def _json_or_empty(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}

def _pr_view(item: Any, state: str | None = None) -> dict[str, Any]:
    try:
        if not isinstance(item, dict):
            raise TypeError
        if state is None:
            state = "merged" if item.get("merged_at") else item["state"]
        result = {
            "number": item["number"],
            "url": item["html_url"],
            "state": state,
            "head": item["head"]["sha"],
        }
        if state == "merged" and item.get("merge_commit_sha") is not None:
            result["merge_sha"] = item["merge_commit_sha"]
        return result
    except (KeyError, TypeError, AttributeError):
        raise GitHubError("github malformed reply") from None

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _default_opener(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            status = getattr(response, "status", None) or response.getcode()
            return int(status), _json_or_empty(response.read())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        return int(exc.code), _json_or_empty(raw)
    except (urllib.error.URLError, OSError):
        raise GitHubError("github unreachable") from None

def _default_git_runner(
    args: list[str], cwd: str, env: dict[str, str]
) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return proc.returncode, proc.stderr

class RestGitHub:
    """GitHub REST adapter using a keeper-owned token file."""

    def __init__(
        self,
        repo: str,
        token_file: str | Path,
        *,
        api: str = "https://api.github.com",
        remote: str | None = None,
        opener: Opener | None = None,
        git_runner: GitRunner | None = None,
    ) -> None:
        if not _REPO_RE.fullmatch(repo):
            raise ValueError("repo must be owner/name")
        self.repo = repo
        self.token_file = Path(token_file)
        self.api = api.rstrip("/")
        self.remote = remote or f"https://github.com/{repo}.git"
        self.opener = opener or _default_opener
        self.git_runner = git_runner or _default_git_runner

    def _token(self) -> str:
        try:
            if self.token_file.stat().st_mode & 0o077:
                raise GitHubError("github token file unavailable")
            token = self.token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            raise GitHubError("github token file unavailable") from None
        if not token:
            raise GitHubError("github token file unavailable")
        return token

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "plangraph-keeper",
        }

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        headers = self._headers(self._token())
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, parsed = self.opener(method, f"{self.api}{path}", headers, data)
        if status >= 300:
            raise GitHubError(f"github {method} {path} -> {status}")
        return parsed

    def find_pr(self, branch: str) -> dict[str, Any] | None:
        owner = self.repo.split("/", 1)[0]
        head = urllib.parse.quote(f"{owner}:{branch}", safe="")
        path = f"/repos/{self.repo}/pulls?head={head}&state=all&per_page=1"
        items = self._request("GET", path)
        if not isinstance(items, list):
            raise GitHubError("github malformed reply")
        if not items:
            return None
        return _pr_view(items[0])

    def open_pr(self, branch: str, base: str, title: str, body: str) -> dict[str, Any]:
        item = self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body},
        )
        return _pr_view(item, state="open")

    def ci_green(self, head: str) -> bool:
        sha = urllib.parse.quote(head, safe="")
        exact: list[dict[str, Any]] = []
        fetched = 0
        total_count: int | None = None
        for page in range(1, 11):
            suffix = "?per_page=100"
            if page > 1:
                suffix += f"&page={page}"
            data = self._request(
                "GET", f"/repos/{self.repo}/commits/{sha}/check-runs{suffix}"
            )
            if not isinstance(data, dict):
                return False
            runs = data.get("check_runs") or []
            if not isinstance(runs, list):
                return False
            if total_count is None:
                tc = data.get("total_count")
                if isinstance(tc, int):
                    total_count = tc
            for run in runs:
                if not isinstance(run, dict) or run.get("name") != "ci-gate":
                    continue
                if run.get("head_sha") != head:
                    continue
                exact.append(run)
            fetched += len(runs)
            if len(runs) < 100:
                break
            if total_count is not None and fetched >= total_count:
                break
        if not exact:
            return False
        newest = max(
            exact,
            key=lambda run: run.get("id") if isinstance(run.get("id"), int) else -1,
        )
        return (
            newest.get("status") == "completed"
            and newest.get("conclusion") == "success"
        )

    def merge_pr(self, number: int, head: str, *, commit_message: str | None = None) -> dict[str, Any]:
        path = f"/repos/{self.repo}/pulls/{number}/merge"
        body = {"sha": head, "merge_method": "merge"}
        if commit_message is not None:
            body.update(commit_title=commit_message.splitlines()[0], commit_message=commit_message)
        reply = self._request("PUT", path, body)
        if not isinstance(reply, dict) or reply.get("merged") is not True:
            raise GitHubError("github merge not confirmed")
        merge_sha = reply.get("sha")
        if not isinstance(merge_sha, str) or re.fullmatch(r"[0-9a-f]{40}", merge_sha) is None:
            raise GitHubError("github merge sha is invalid")
        return {
            "number": number,
            "url": f"https://github.com/{self.repo}/pull/{number}",
            "state": "merged",
            "head": head,
            "merge_sha": merge_sha,
        }

    def push_branch(self, repo_root: Path, branch: str, head: str) -> None:
        token = self._token()
        basic = base64.b64encode(
            f"x-access-token:{token}".encode("utf-8")
        ).decode("ascii")
        header = "Authorization: Basic " + basic
        # Token only travels in git config env, never in argv or error text.
        env = {
            name: os.environ[name]
            for name in (
                "PATH",
                "HOME",
                "LANG",
                "LC_ALL",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "GIT_SSL_CAINFO",
            )
            if name in os.environ
        }
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": header,
            }
        )
        args = ["git", "push", self.remote, f"{head}:refs/heads/{branch}"]
        try:
            code, _stderr = self.git_runner(args, str(repo_root), env)
        except (OSError, subprocess.SubprocessError):
            raise GitHubError("git push failed") from None
        if code != 0:
            raise GitHubError("git push failed")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> RestGitHub | None:
        repo = environ.get("PG_PHASE_GITHUB_REPO", "").strip()
        token_file = environ.get("PG_PHASE_GITHUB_TOKEN_FILE", "").strip()
        if not repo or not token_file:
            return None
        return cls(repo, token_file)
