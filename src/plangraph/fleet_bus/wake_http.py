"""HTTP seams for the fleet-bus wake hook.

Two pluggable pieces, both free of side effects at import time and free of
default server endpoints (callers supply base URLs from their config):

* :class:`SyntheticSink` -- wakes a session through its synthetic/inbox
  endpoint.  OpenCode sessions take ``POST /api/session/<id>/synthetic`` with
  ``{"text": ..., "resume": true}``, which resumes the session WITHOUT a
  visible user bubble (the routing nudge's ``/prompt`` shape adds one, so it
  is deliberately not used here).  Claude sessions expose a different
  synthetic/inbox path; their caller passes ``paths={"claude": ...}``.

* :class:`KeeperMessagesPoll` -- long-polls the keeper messages API
  (``GET /v1/messages?since=<cursor>&wait_ms=...``) so a new ``messages``
  row wakes a session with no timer anywhere.

Both are duck-typed so unit tests can inject fake transports.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from .wake import PollFailed, SessionTarget, WakeBusy, WakeRefused

__all__ = [
    "OPENCODE_SYNTHETIC_PATH",
    "SyntheticSink",
    "KeeperMessagesPoll",
]

#: OpenCode's synthetic endpoint: wakes a session with no user bubble.
OPENCODE_SYNTHETIC_PATH = "/api/session/{session}/synthetic"

#: Default per-kind paths.  Claude and other session kinds are added by the
#: caller because their inbox paths are owned by the routing side.
DEFAULT_PATHS: Dict[str, str] = {"opencode": OPENCODE_SYNTHETIC_PATH}

#: Cap on how much of a response body we read (a woken session can stream).
READ_CAP = 65536


def _request(
    data: Optional[bytes],
    url: str,
    timeout: float,
    headers: Mapping[str, str],
) -> Tuple[int, bytes]:
    """Open a URL and return ``(status, body)``; transport errors raise OSError."""
    req = urllib.request.Request(url, data=data, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status), resp.read(READ_CAP)
    except urllib.error.HTTPError as exc:  # non-2xx still carries a status
        return int(exc.code), exc.read(READ_CAP)


def _get(url: str, timeout: float, headers: Mapping[str, str]) -> Tuple[int, bytes]:
    return _request(None, url, timeout, headers)


def _post(
    url: str,
    body: Mapping[str, Any],
    timeout: float,
    headers: Mapping[str, str],
) -> Tuple[int, bytes]:
    raw = json.dumps(dict(body)).encode("utf-8")
    return _request(raw, url, timeout, headers)


class SyntheticSink:
    """Wake sessions through their synthetic/inbox endpoint.

    ``post`` is an injectable transport with the signature
    ``(url, body, timeout, headers) -> (status, body)``; it defaults to a
    real ``urllib`` POST.  ``paths`` maps a session kind (``opencode``,
    ``claude``, ...) to a URL path template containing ``{session}``.
    """

    def __init__(
        self,
        paths: Optional[Mapping[str, str]] = None,
        post: Optional[Callable[..., Tuple[int, bytes]]] = None,
        authorization: str = "",
        timeout: float = 10.0,
    ) -> None:
        merged = dict(DEFAULT_PATHS)
        if paths:
            merged.update(paths)
        self.paths: Dict[str, str] = merged
        self.post = post if post is not None else _post
        self.authorization = authorization
        self.timeout = timeout

    # -- interface -----------------------------------------------------
    def wake(self, target: SessionTarget, text: str) -> None:
        """POST ``text`` to the target's synthetic endpoint.

        Raises :class:`WakeRefused` when the target cannot be reached at all
        (unknown kind, empty base, HTTP 404) and :class:`WakeBusy` for any
        transient failure (409/423/429/5xx/transport), which the hook
        retries later -- a busy target is never treated as a drop.
        """
        if not target.base:
            raise WakeRefused(f"{target.kind}/{target.session}: no base url")
        path = self.paths.get(target.kind)
        if not path:
            raise WakeRefused(f"{target.kind}: no synthetic path configured")
        url = target.base.rstrip("/") + path.format(session=target.session)
        body: Dict[str, Any] = {"text": text, "resume": True}
        headers = {"Content-Type": "application/json"}
        if self.authorization:
            headers["Authorization"] = self.authorization
        try:
            status, _raw = self.post(url, body, self.timeout, headers)
        except OSError as exc:
            raise WakeBusy(f"{target.kind}/{target.session}: {exc}") from exc
        if status in (200, 201, 204):
            return
        if status == 404:
            raise WakeRefused(f"{target.kind}/{target.session}: no such session")
        raise WakeBusy(f"{target.kind}/{target.session}: http {status}")


class KeeperMessagesPoll:
    """Long-poll client for ``GET /v1/messages?since=<cursor>&wait_ms=...``.

    ``get`` is an injectable transport with the signature
    ``(url, timeout, headers) -> (status, body)``; it defaults to a real
    ``urllib`` GET.  ``token`` is the bearer token (read from the token file
    by the caller, never written to disk here).  The instance is itself the
    ``poll(since, wait_ms)`` callable ``WakeHook.run`` expects.
    """

    def __init__(
        self,
        base: str,
        token: str,
        get: Optional[Callable[..., Tuple[int, bytes]]] = None,
    ) -> None:
        if not base:
            raise ValueError("keeper messages poll needs a base url")
        if not token:
            raise ValueError("keeper messages poll needs a bearer token")
        self.base = base.rstrip("/")
        self.token = token
        self.get = get if get is not None else _get

    # -- interface -----------------------------------------------------
    def __call__(self, since, wait_ms: int) -> List[Dict[str, Any]]:
        return self.poll(since, wait_ms)

    def poll(self, since, wait_ms: int) -> List[Dict[str, Any]]:
        """Return keeper message rows newer than ``since``.

        Raises :class:`PollFailed` on transport/HTTP/shape errors; the hook
        keeps its cursor and retries, so a failed poll never loses rows.
        """
        url = (
            f"{self.base}/v1/messages?since={quote(str(since), safe='')}"
            f"&wait_ms={int(wait_ms)}"
        )
        headers = {"Authorization": f"Bearer {self.token}"}
        timeout = max(1.0, wait_ms / 1000.0 + 5.0)
        try:
            status, raw = self.get(url, timeout, headers)
        except OSError as exc:
            raise PollFailed(f"messages poll transport error: {exc}") from exc
        if status != 200:
            raise PollFailed(f"messages poll http {status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PollFailed(f"messages poll not json: {exc}") from exc
        if not isinstance(payload, dict):
            raise PollFailed("messages poll: payload is not an object")
        if payload.get("error"):
            raise PollFailed(f"messages poll error: {payload['error']}")
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise PollFailed("messages poll: missing rows list")
        return [r for r in rows if isinstance(r, dict)]
