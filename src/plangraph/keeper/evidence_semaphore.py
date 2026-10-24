"""Semaphore-task adapter for evidence kind ``semaphore-task``.

Endpoint: ``GET {base}/api/project/{project_id}/tasks/{task_id}`` (Semaphore UI
API, bearer token). Both ids are positive integers; the project must be listed in
``$PG_SEMAPHORE_PROJECTS`` (fail closed). The real fetcher takes the base URL
from ``$PG_SEMAPHORE_URL`` and the bearer token from the file named by
``$PG_SEMAPHORE_TOKEN_FILE``. The token is read per call and never stored on the
adapter, never logged, and never included in a raised message or in ``repr``.

The base URL must be a plain ``http(s)`` URL with no embedded credentials.
Redirects are refused outright: a redirect would otherwise replay the
``Authorization`` header at the target. The response body is capped at 1 MiB.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

__all__ = ["SemaphoreAdapter", "SemaphoreError"]

_MAX_RESPONSE_BYTES = 1024 * 1024


class SemaphoreError(RuntimeError):
    """A failure that is safe to show: it never carries the token."""


_MAX_ID = 2**63 - 1  # Semaphore ids are int64; anything larger is not an id.


def positive_int(value: Any) -> int | None:
    """``value`` as a positive int64 (int or digit-only string), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not (value.isascii() and value.isdigit() and len(value) <= 19):
            return None
        value = int(value)
    if isinstance(value, int) and 0 < value <= _MAX_ID:
        return value
    return None


def _allowed_projects() -> frozenset[int]:
    """``$PG_SEMAPHORE_PROJECTS`` (comma-separated positive ints); fail closed."""
    raw = os.environ.get("PG_SEMAPHORE_PROJECTS", "")
    ids = [positive_int(part.strip()) for part in raw.split(",")] if raw.strip() else []
    if not ids or any(i is None for i in ids):
        raise SemaphoreError("semaphore project allowlist not configured")
    return frozenset(ids)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect before the request (and its token) is replayed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Deliberately opaque: never mention the token or the redirect target.
        raise SemaphoreError("redirect refused")


class SemaphoreAdapter:
    """Injected through ``ctx.semaphore``; tests pass a fake fetcher."""

    def __init__(
        self,
        fetch: Callable[[int, int], dict] | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._fetch = fetch or self._http_fetch
        self._timeout = float(timeout)

    def get(self, task_id: Any, project_id: Any) -> dict:
        task, project = positive_int(task_id), positive_int(project_id)
        if task is None or project is None:
            raise SemaphoreError("task_id and project_id must be positive integers")
        return self._fetch(task, project)

    def readable(self, project_id: Any) -> bool:
        """True when the project is publicly readable (on the allow list).

        Fail closed: an unconfigured allow list, an invalid id or any
        unexpected error is not readable, never an exception.
        """
        project = positive_int(project_id)
        if project is None:
            return False
        try:
            allowed = _allowed_projects()
        except Exception:  # noqa: BLE001 - fail closed on anything unexpected
            return False
        return project in allowed

    def __repr__(self) -> str:
        # Deliberately opaque: never mention the base URL or a token.
        return "SemaphoreAdapter()"

    @staticmethod
    def _opener() -> urllib.request.OpenerDirector:
        # An empty ProxyHandler: build_opener would otherwise add one that reads
        # HTTP(S)_PROXY from the environment and send the bearer through it.
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    # -- real fetcher -------------------------------------------------------
    def _http_fetch(self, task_id: int, project_id: int) -> dict:
        base = os.environ.get("PG_SEMAPHORE_URL", "").rstrip("/")
        token_file = os.environ.get("PG_SEMAPHORE_TOKEN_FILE", "")
        if not base:
            raise SemaphoreError("PG_SEMAPHORE_URL is not set")
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise SemaphoreError("PG_SEMAPHORE_URL must be an http(s) URL")
        if parts.username or parts.password:
            raise SemaphoreError("PG_SEMAPHORE_URL must not contain credentials")
        if not token_file:
            raise SemaphoreError("PG_SEMAPHORE_TOKEN_FILE is not set")
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            raise SemaphoreError("cannot read the semaphore token file") from None
        if not token:
            raise SemaphoreError("the semaphore token file is empty")

        allowed = _allowed_projects()
        if project_id not in allowed:
            raise SemaphoreError("project not allowed")

        url = (
            f"{base}/api/project/{quote(str(project_id), safe='')}"
            f"/tasks/{quote(str(task_id), safe='')}"
        )
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with self._opener().open(request, timeout=self._timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except SemaphoreError:
            raise
        except urllib.error.HTTPError as exc:
            code = exc.code if isinstance(exc.code, int) else 0
            if code in (401, 403):
                raise SemaphoreError(f"semaphore auth refused ({code})") from None
            raise SemaphoreError(f"semaphore request failed (HTTP {code})") from None
        except Exception as exc:  # noqa: BLE001 - sanitise every transport error
            # `from None` drops the cause, whose text may contain the token.
            raise SemaphoreError(
                f"semaphore request failed ({type(exc).__name__})"
            ) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise SemaphoreError("semaphore response was too large")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise SemaphoreError("semaphore response was not UTF-8") from None
        try:
            data: Any = json.loads(text)
        except ValueError:
            raise SemaphoreError("semaphore response was not JSON") from None
        if not isinstance(data, dict):
            raise SemaphoreError("semaphore response was not a JSON object")
        return data
