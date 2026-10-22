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

def _default_opener(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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
        if status >= 400:
            raise GitHubError(f"github {method} {path} -> {status}")
        return parsed

    def find_pr(self, branch: str) -> dict[str, Any] | None:
        owner = self.repo.split("/", 1)[0]
        head = urllib.parse.quote(f"{owner}:{branch}", safe="")
        path = f"/repos/{self.repo}/pulls?head={head}&state=all&per_page=1"
        items = self._request("GET", path)
        if not isinstance(items, list) or not items:
            return None
        item = items[0]
        state = "merged" if item.get("merged_at") else item["state"]
        return {
            "number": item["number"],
            "url": item["html_url"],
            "state": state,
            "head": item["head"]["sha"],
        }

    def open_pr(self, branch: str, base: str, title: str, body: str) -> dict[str, Any]:
        item = self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body},
        )
        return {
            "number": item["number"],
            "url": item["html_url"],
            "state": "open",
            "head": item["head"]["sha"],
        }

    def ci_green(self, head: str) -> bool:
        sha = urllib.parse.quote(head, safe="")
        data = self._request(
            "GET", f"/repos/{self.repo}/commits/{sha}/check-runs?per_page=100"
        )
        if not isinstance(data, dict):
            return False
        for run in data.get("check_runs") or []:
            if not isinstance(run, dict) or run.get("name") != "ci-gate":
                continue
            run_sha = run.get("head_sha")
            if run_sha is not None and run_sha != head:
                continue
            if run.get("status") == "completed" and run.get("conclusion") == "success":
                return True
        return False

    def merge_pr(self, number: int, head: str) -> dict[str, Any]:
        path = f"/repos/{self.repo}/pulls/{number}/merge"
        self._request("PUT", path, {"sha": head, "merge_method": "merge"})
        return {
            "number": number,
            "url": f"https://github.com/{self.repo}/pull/{number}",
            "state": "merged",
            "head": head,
        }

    def push_branch(self, repo_root: Path, branch: str, head: str) -> None:
        token = self._token()
        basic = base64.b64encode(
            f"x-access-token:{token}".encode("utf-8")
        ).decode("ascii")
        header = "Authorization: Basic " + basic
        # Token only travels in git config env, never in argv or error text.
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": header,
        }
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
