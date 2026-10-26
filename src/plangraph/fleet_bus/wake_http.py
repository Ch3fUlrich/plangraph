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

Both are duck-typed so unit tests can inject fake transports.  When a real
socket opens (a redirected endpoint, the V7-4 keeper long-poll test), both go
through :func:`_request`, which follows a redirect only on the same origin so
the bearer token is never forwarded to another host (G3-7).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote, urljoin, urlsplit

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

#: Same logger as :mod:`.wake` so the whole wake subsystem logs in one stream.
_LOGGER = logging.getLogger("plangraph.fleet_bus.wake")


class _CrossOriginRefused(OSError):
    """A redirect hop crossed origins and was refused (the bearer stays home)."""


def _origin(url: str) -> Tuple[str, str, Optional[int]]:
    """``(scheme, host, effective port)`` of *url*, lower-cased."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or {"http": 80, "https": 443}.get(scheme)
    return scheme, (parts.hostname or "").lower(), port


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only while it stays on the request's own origin.

    Both seams here send ``Authorization`` (G3-7): a hop to another
    scheme/host/port would hand that bearer token to a host that never earned
    it, so the 3xx is handed back to the caller as a status instead of being
    followed -- ``SyntheticSink`` reports it as busy and ``KeeperMessagesPoll``
    keeps its cursor, so a redirect can delay a wake but never leak the token
    and never drop the row.  A refused hop is recorded on the instance so
    :func:`_request` can tell it from any other 3xx.

    A wake POST is never downgraded either: ``urllib`` re-issues a redirected
    301/302/303 POST as a body-less GET (the wake payload vanishes), so those
    come back as a status too; only 307/308 keep method and body and are
    followed as a POST.  A GET (the keeper poll) keeps the ``urllib`` behavior.
    """

    def __init__(self) -> None:
        super().__init__()
        self.foreign_hop = False

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        try:
            foreign = _origin(target) != _origin(req.full_url)
        except ValueError:  # a malformed port makes the hop unverifiable
            foreign = True
        if foreign:
            self.foreign_hop = True
            return None
        if req.data is not None:  # a POST wake must never lose its body
            if code not in (307, 308):
                return None
            kept = {k: v for k, v in req.headers.items()
                    if k.lower() != "content-length"}  # recomputed on send
            return urllib.request.Request(
                target, data=req.data, headers=kept, method="POST")
        return super().redirect_request(req, fp, code, msg, headers, target)


def _request(
    data: Optional[bytes],
    url: str,
    timeout: float,
    headers: Mapping[str, str],
) -> Tuple[int, bytes]:
    """Open a URL and return ``(status, body)``; transport errors raise OSError."""
    redirects = _SameOriginRedirects()
    opener = urllib.request.build_opener(redirects)
    try:
        req = urllib.request.Request(url, data=data, headers=dict(headers))
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status), resp.read(READ_CAP)
    except urllib.error.HTTPError as exc:  # non-2xx (incl. a refused hop) carries a status
        if redirects.foreign_hop:
            raise _CrossOriginRefused(
                f"cross-origin redirect refused: {url}") from exc
        return int(exc.code), exc.read(READ_CAP)
    except ValueError as exc:  # malformed port & friends stay OSError
        raise OSError(f"bad request url: {exc}") from exc


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
    ``cross_origin_max`` bounds consecutive cross-origin redirect refusals.
    """

    def __init__(
        self,
        paths: Optional[Mapping[str, str]] = None,
        post: Optional[Callable[..., Tuple[int, bytes]]] = None,
        authorization: str = "",
        timeout: float = 10.0,
        cross_origin_max: int = 3,
    ) -> None:
        merged = dict(DEFAULT_PATHS)
        if paths:
            merged.update(paths)
        self.paths: Dict[str, str] = merged
        self.post = post if post is not None else _post
        self.authorization = authorization
        self.timeout = timeout
        self.cross_origin_max = cross_origin_max
        self._cross_origin: Dict[Tuple[str, str], int] = {}

    # -- interface -----------------------------------------------------
    def wake(self, target: SessionTarget, text: str) -> None:
        """POST ``text`` to the target's synthetic endpoint.

        Raises :class:`WakeRefused` when the target cannot be reached at all
        (unknown kind, empty base, HTTP 404) and :class:`WakeBusy` for any
        transient failure (409/423/429/5xx/transport), which the hook
        retries later -- a busy target is never treated as a drop.  A wake
        kept busy by a cross-origin redirect for ``cross_origin_max``
        consecutive tries is refused instead (logged once).
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
        key = (target.kind, target.session)
        try:
            status, _raw = self.post(url, body, self.timeout, headers)
        except _CrossOriginRefused as exc:
            misses = self._cross_origin.get(key, 0) + 1
            self._cross_origin[key] = misses
            why = f"{target.kind}/{target.session}: cross-origin redirect"
            if misses < self.cross_origin_max:
                raise WakeBusy(why) from exc
            if misses == self.cross_origin_max:  # give up once, not per retry
                _LOGGER.warning(
                    "wake for %s/%s refused after %d cross-origin redirects",
                    target.kind, target.session, misses)
            raise WakeRefused(why) from exc
        except OSError as exc:
            self._cross_origin.pop(key, None)
            raise WakeBusy(f"{target.kind}/{target.session}: {exc}") from exc
        self._cross_origin.pop(key, None)  # any other outcome resets the count
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
