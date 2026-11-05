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
