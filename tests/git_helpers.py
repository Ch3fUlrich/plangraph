"""Hermetic ``git`` helpers shared by the evidence tests.

``git`` reads ~/.gitconfig, the XDG config and the system config
(commit.gpgsign, init.templateDir, includeIf, ...) which differ per machine,
so :func:`git_cli` runs every call - ``git init`` included - with
``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` pointed at one empty file and
``HOME`` under the test's ``tmp_path``, and with the ambient ``GIT_*``
variables scrubbed so the repo-local user.name/user.email decide the
committer identity.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
import re
import subprocess
from pathlib import Path
from typing import Callable


def git_cli(tmp_path: Path, monkeypatch=None) -> tuple[Callable[..., str], Callable[..., None]]:
    """Return ``(git, git_init)`` runners bound to the hermetic env above.

    With *monkeypatch* the same env is applied to the process, so the code
    under test (GitAdapter runs git with the inherited env) is hermetic too.

    ``git(repo, *args)`` runs ``git -C repo ...`` and returns stripped stdout;
    ``git_init(repo, *args)`` runs ``git init *args repo``.
    """
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(empty)
    env["GIT_CONFIG_SYSTEM"] = str(empty)
    env["HOME"] = str(tmp_path)
    if monkeypatch is not None:
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            monkeypatch.delenv(key)
        for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HOME"):
            monkeypatch.setenv(key, env[key])

    def _git(repo: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, check=True, env=env,
        )
        return proc.stdout.strip()

    def _git_init(repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "init", *args, str(repo)],
            capture_output=True, text=True, check=True, env=env,
        )

    return _git, _git_init


def git_supports_init_branch() -> bool:
    """True when the installed git supports ``git init -b`` (git >= 2.28)."""
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    match = re.search(r"(\d+)\.(\d+)", proc.stdout)
    return bool(match) and tuple(map(int, match.groups())) >= (2, 28)


def decisions_git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, timeout=30).stdout.strip()


@pytest.fixture
def data_repos(tmp_path, monkeypatch, request):
    from plangraph.keeper import evidence
    monkeypatch.chdir(tmp_path)
    # Ignore caller hooks/config and repository-local Git environment, including
    # the context inherited when these tests run inside the pre-push hook.
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    for name, value in {
        "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
    }.items():
        monkeypatch.setenv(name, value)
    origin = tmp_path / "origin.git"
    decisions_git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    clone = tmp_path / "data"
    decisions_git(tmp_path, "clone", str(origin), str(clone))
    relative = getattr(request, "param", "router/docs/decisions-log.md")
    log = clone / relative
    log.parent.mkdir(parents=True)
    initial = "| D-1 | demo | Initial decision |\n"
    log.write_text(initial, encoding="utf-8")
    decisions_git(clone, "add", ".")
    decisions_git(clone, "commit", "-m", "Initial decisions")
    decisions_git(clone, "push", "origin", "main")
    base = decisions_git(clone, "rev-parse", "HEAD")
    worktree = tmp_path / "writer"
    decisions_git(clone, "worktree", "add", "-b", "decisions-update", str(worktree))
    return SimpleNamespace(
        origin=origin, clone=clone, log=log, relative=relative,
        worktree=worktree, base=base, initial=initial,
        ctx=evidence.default_context({"decisions_file": str(log)}))


@contextmanager
def authenticated_git_origin(tmp_path):
    """Loopback smart-HTTP origin requiring only fake fixture credentials."""
    import base64
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit
    wanted = 'Basic ' + base64.b64encode(b'fixture-user:fixture-password').decode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.serve()
        def do_POST(self):
            self.serve()
        def serve(self):
            assert self.headers.get('X-Fixture-Secret') is None
            if self.headers.get('Authorization') != wanted:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="fixture"')
                self.end_headers()
                return
            url = urlsplit(self.path)
            size = int(self.headers.get('Content-Length', 0))
            env = dict(os.environ, GIT_PROJECT_ROOT=str(tmp_path), GIT_HTTP_EXPORT_ALL='1',
                       GIT_CONFIG_GLOBAL=os.devnull, REQUEST_METHOD=self.command, PATH_INFO=url.path,
                       QUERY_STRING=url.query, CONTENT_TYPE=self.headers.get('Content-Type', ''),
                       CONTENT_LENGTH=str(size), REMOTE_USER='fixture-user')
            proc = subprocess.run(['git', 'http-backend'], cwd=tmp_path, env=env,
                                  input=self.rfile.read(size), capture_output=True, timeout=5)
            headers, _, body = proc.stdout.partition(b'\r\n\r\n')
            self.send_response(200)
            for line in headers.splitlines():
                key, _, value = line.decode().partition(':')
                self.send_header(key, value.strip())
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/origin.git'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
