"""Fixture git identity for tests.

Tests must set git identity only via env (identity_env) or -c (identity_args).
Local config writes to user.name / user.email are forbidden.
"""
from __future__ import annotations

import os

DEFAULT_NAME = "Test User"
DEFAULT_EMAIL = "test@example.invalid"

IDENTITY_ENV: dict[str, str] = {
    "GIT_AUTHOR_NAME": DEFAULT_NAME,
    "GIT_AUTHOR_EMAIL": DEFAULT_EMAIL,
    "GIT_COMMITTER_NAME": DEFAULT_NAME,
    "GIT_COMMITTER_EMAIL": DEFAULT_EMAIL,
}


def identity_env(name: str | None = None, email: str | None = None) -> dict[str, str]:
    """Return a copy of os.environ with git author/committer identity set."""
    n = name if name is not None else DEFAULT_NAME
    e = email if email is not None else DEFAULT_EMAIL
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": n,
        "GIT_AUTHOR_EMAIL": e,
        "GIT_COMMITTER_NAME": n,
        "GIT_COMMITTER_EMAIL": e,
    }


def identity_args(name: str | None = None, email: str | None = None) -> list[str]:
    """Return ['-c', 'user.name=...', '-c', 'user.email=...'] for single git commands."""
    n = name if name is not None else DEFAULT_NAME
    e = email if email is not None else DEFAULT_EMAIL
    return ["-c", f"user.name={n}", "-c", f"user.email={e}"]
