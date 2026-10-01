"""Semaphore-task adapter for evidence kind ``semaphore-task``.

The API shape is UNVERIFIED (docs/KEEPER.md says so): ship with an injected
fetcher and verify the live shape later. The real fetcher takes the base URL
from ``$PG_SEMAPHORE_URL`` and the bearer token from the file named by
``$PG_SEMAPHORE_TOKEN_FILE``. The token is read per call and never stored on the
adapter, never logged, and never included in a raised message or in ``repr``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

__all__ = ["SemaphoreAdapter", "SemaphoreError"]


class SemaphoreError(RuntimeError):
    """A failure that is safe to show: it never carries the token."""


class SemaphoreAdapter:
    """Injected through ``ctx.semaphore``; tests pass a fake fetcher."""

    def __init__(
        self,
        fetch: Callable[[str], dict] | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._fetch = fetch or self._http_fetch
        self._timeout = float(timeout)

    def get(self, task_id: str) -> dict:
        return self._fetch(str(task_id))

    def __repr__(self) -> str:
        # Deliberately opaque: never mention the base URL or a token.
        return "SemaphoreAdapter()"

    # -- real fetcher -------------------------------------------------------
    def _http_fetch(self, task_id: str) -> dict:
        base = os.environ.get("PG_SEMAPHORE_URL", "").rstrip("/")
        token_file = os.environ.get("PG_SEMAPHORE_TOKEN_FILE", "")
        if not base:
            raise SemaphoreError("PG_SEMAPHORE_URL is not set")
        if not token_file:
            raise SemaphoreError("PG_SEMAPHORE_TOKEN_FILE is not set")
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            raise SemaphoreError("cannot read the semaphore token file") from None
        if not token:
            raise SemaphoreError("the semaphore token file is empty")

        url = f"{base}/api/tasks/{quote(task_id, safe='')}"
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - sanitise every transport error
            # `from None` drops the cause, whose text may contain the token.
            raise SemaphoreError(
                f"semaphore request failed ({type(exc).__name__})"
            ) from None
        try:
            data: Any = json.loads(raw)
        except ValueError:
            raise SemaphoreError("semaphore response was not JSON") from None
        if not isinstance(data, dict):
            raise SemaphoreError("semaphore response was not a JSON object")
        return data
