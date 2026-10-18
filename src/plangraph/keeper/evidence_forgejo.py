"""Forgejo commit-status adapter for the ``commit+ci`` verifier.

A ``ci`` value of ``forgejo:<owner>/<repo>`` is looked up through the Forgejo
statuses endpoint instead of ``gh``:

* path ``/repos/{owner}/{repo}/commits/{ref}/statuses`` (api json line 3),
  operationId ``repoListStatusesByRef`` (line 11);
* paging through the 1-based ``page`` (line 63) and ``limit`` (line 69) query
  parameters — the excerpt documents no ``Link`` header, so a short page is the
  only "last page" signal there is; ``sort`` (line 44) stays default so the
  newest-per-context rule below is decided locally, not by server ordering;
* ``Authorization: token <T>`` (securityDefinitions, lines 150-154: "API tokens
  must be prepended with ``token`` followed by a space").

The token is read from ``token_file`` at call time, refused when the file is
missing, empty, a symlink or not a single token, and it is never stored on the
adapter, never put in the URL, argv, a log line, an exception message or
``repr``. Requests go through stdlib :mod:`urllib.request` (no subprocess, no
``curl``), over https — plain http is accepted only for ``127.0.0.1``, which is
what the hermetic tests bind — with a hard timeout, a response-size cap read in
chunks and a ``max_pages`` cap: a page of exactly ``page_size`` items at
``max_pages`` raises instead of returning a truncated list (fail closed).

Each ``CommitStatus`` (``id``, ``status``, ``context``, ``created_at``,
``updated_at`` — api json lines 89-117) is normalised by the pure
:func:`normalise_statuses` into the check-run shape the verifier already
consumes, keeping only the newest status per ``context`` so the stale failure
of a rerun cannot block a green sha.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from .evidence_ci import _SLUG_RE
from .evidence_types import valid_commit_ref

__all__ = ["ForgejoStatusAdapter", "normalise_statuses"]

#: The ``ci`` value prefix the CI adapter delegates on.
PREFIX = "forgejo:"

# CommitStatusState (api json line 130: "pending", "success", "error",
# "failure", "warning", or "skipped") -> the check-run conclusion it means.
# Anything not named here is unknown and fails closed as a failure.
_CONCLUSIONS = {
    "success": "success",
    "failure": "failure",
    "error": "failure",
    "warning": "neutral",
    "skipped": "skipped",
    "pending": None,
}
#: Statuses the API only reports once the job is finished, so the normalised
#: run may be read as ``completed``.
_COMPLETED_STATUSES = frozenset(
    {"success", "error", "failure", "warning", "skipped"}
)

_CHUNK = 65536


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect before the request (and its token) is replayed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Deliberately opaque: no target, no token.
        raise RuntimeError("forgejo statuses redirect refused")


def _default_opener() -> urllib.request.OpenerDirector:
    # An empty ProxyHandler: build_opener would otherwise read HTTP(S)_PROXY
    # from the environment and hand the Authorization header to a proxy.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect()
    )


def _rank(entry: dict) -> tuple[str, int]:
    """Sort key for "newest status": highest ``updated_at``, then highest id."""
    updated = entry.get("updated_at")
    updated_s = updated if isinstance(updated, str) else ""
    raw_id = entry.get("id")
    id_int = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else 0
    return (updated_s, id_int)


def normalise_statuses(sha: str, statuses: Any) -> list[dict]:
    """Map a ``CommitStatus`` list onto check-run-shaped dicts (pure, no I/O).

    Only the newest status per ``context`` survives — the API lists every
    status ever posted for the ref, so an old ``failure`` of a rerun would
    otherwise block a sha that is green now; ties on ``updated_at`` go to the
    higher ``id``. An unknown ``status`` value becomes conclusion ``failure``
    (fail closed), ``pending`` stays in-progress with no conclusion, and a
    status without a usable ``context`` is kept under its own key so it can
    never be merged into — or masked by — another context. Entries that are not
    dicts are dropped (the fetch refuses them earlier); an empty input is an
    empty list, which the verifier already fails on.
    """
    if not isinstance(statuses, list):
        return []
    newest: dict[Any, dict] = {}
    for index, entry in enumerate(statuses):
        if not isinstance(entry, dict):
            continue
        context = entry.get("context")
        if isinstance(context, str) and context:
            key: Any = context
        else:
            # Unique, never collides with a real context.
            key = ("__no_context__", index)
        kept = newest.get(key)
        if kept is None or _rank(entry) > _rank(kept):
            newest[key] = entry
    out: list[dict] = []
    for key, entry in newest.items():
        raw_status = entry.get("status")
        status = (
            raw_status.strip().lower()
            if isinstance(raw_status, str)
            else ""
        )
        conclusion = _CONCLUSIONS.get(status, "failure")
        context = entry.get("context")
        out.append(
            {
                "id": entry.get("id"),
                "name": context if isinstance(context, str) else "",
                "head_sha": sha,
                "app": {"slug": "forgejo-actions"},
                "started_at": entry.get("created_at"),
                "status": "completed" if status in _COMPLETED_STATUSES else "in_progress",
                "conclusion": conclusion,
            }
        )
    out.sort(key=lambda run: (str(run["name"]), _id_sortable(run.get("id"))))
    return out


def _id_sortable(value: Any) -> int:
    """Ids rank as ints (bool excluded, digit strings allowed), else 0."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


class ForgejoStatusAdapter:
    """``forgejo:<owner>/<repo>`` CI values → normalised check-runs.

    Injected into :class:`~plangraph.keeper.evidence_ci.CIAdapter` as
    ``forgejo=``; tests point ``base_url`` at a ``127.0.0.1`` server.
    """

    def __init__(
        self,
        base_url: str,
        token_file: str,
        *,
        timeout: float = 15.0,
        max_pages: int = 10,
        page_size: int = 50,
        max_bytes: int = 1_000_000,
        opener: Any = None,
    ) -> None:
        self._base_url = self._validated_base(base_url)
        self._token_file = str(token_file) if token_file else ""
        self._timeout = float(timeout)
        self._max_pages = max(1, int(max_pages))
        self._page_size = max(1, int(page_size))
        self._max_bytes = max(1, int(max_bytes))
        self._opener = opener if opener is not None else _default_opener()

    # -- configuration guards ---------------------------------------------
    @staticmethod
    def _validated_base(base_url: Any) -> str:
        """A credential-free http(s) base URL; https except for 127.0.0.1."""
        if not isinstance(base_url, str) or not base_url.strip():
            raise RuntimeError("forgejo base url is not configured")
        base = base_url.strip().rstrip("/")
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise RuntimeError("forgejo base url must be an http(s) url")
        if parts.username or parts.password:
            raise RuntimeError("forgejo base url must not contain credentials")
        if parts.scheme != "https" and parts.hostname != "127.0.0.1":
            raise RuntimeError("forgejo base url must use https")
        return base

    def _token(self) -> str:
        """Read the token *now*; refuse missing/empty/symlink/non-single-token."""
        if not self._token_file:
            raise RuntimeError("forgejo token file is not configured")
        path = Path(self._token_file)
        try:
            symlink = path.is_symlink()
        except OSError:
            raise RuntimeError("forgejo token file is not readable") from None
        if symlink:
            raise RuntimeError("forgejo token file must not be a symlink")
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            raise RuntimeError("forgejo token file cannot be read") from None
        if not token:
            raise RuntimeError("forgejo token file is empty")
        if any(char.isspace() for char in token):
            # A newline in the middle would split the header; refuse outright.
            raise RuntimeError("forgejo token file does not hold a single token")
        return token

    def __repr__(self) -> str:
        # Deliberately opaque: never the base URL, the path or the token.
        return "ForgejoStatusAdapter()"

    # -- public ------------------------------------------------------------
    def check_runs(self, repo_slug: str, sha: str) -> list[dict]:
        """Normalised check-runs for *sha* under ``forgejo:<owner>/<repo>``.

        ``repo_slug`` is the ``owner/repo`` part; both it and the sha are
        validated before the token is read and before any request is made, and
        the URL is assembled from those validated components only.
        Raises ``ValueError`` on a bad slug or sha, ``RuntimeError`` on any
        transport, HTTP, size, paging or payload problem — messages carry the
        status code and ``owner/repo`` only, never headers, body or token.
        """
        if not valid_commit_ref(sha):
            raise ValueError("check-runs requires a full lowercase commit sha")
        if not isinstance(repo_slug, str) or not _SLUG_RE.fullmatch(repo_slug):
            raise ValueError("forgejo ci value must be forgejo:owner/repo")
        owner, repo = repo_slug.split("/", 1)
        token = self._token()
        statuses = self._fetch(owner, repo, sha, token)
        return normalise_statuses(sha, statuses)

    # -- transport ---------------------------------------------------------
    def _fetch(self, owner: str, repo: str, sha: str, token: str) -> list:
        quoted = quote(owner, safe=""), quote(repo, safe=""), quote(sha, safe="")
        collected: list[dict] = []
        for page in range(1, self._max_pages + 1):
            url = (
                f"{self._base_url}/api/v1/repos/{quoted[0]}/{quoted[1]}"
                f"/commits/{quoted[2]}/statuses?page={page}&limit={self._page_size}"
            )
            payload = self._get(url, token, owner, repo)
            if not isinstance(payload, list):
                raise RuntimeError(
                    f"forgejo statuses were not a list for {owner}/{repo}"
                )
            if any(not isinstance(entry, dict) for entry in payload):
                raise RuntimeError(
                    f"forgejo statuses were malformed for {owner}/{repo}"
                )
            collected.extend(payload)
            if len(payload) < self._page_size:
                return collected
        # Every fetched page was exactly full at max_pages: there may be more
        # statuses we never saw, so refuse instead of returning a partial list.
        raise RuntimeError(
            f"forgejo statuses truncated for {owner}/{repo} "
            f"({self._max_pages} pages of {self._page_size})"
        )

    def _get(self, url: str, token: str, owner: str, repo: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = self._read_capped(response, owner, repo)
        except urllib.error.HTTPError as exc:
            code = exc.code if isinstance(exc.code, int) else 0
            # `from None` drops the cause: it may quote headers or the body.
            raise RuntimeError(
                f"forgejo statuses request failed for {owner}/{repo} (HTTP {code})"
            ) from None
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - sanitise every transport error
            # `from None` drops the cause, whose text may echo the URL.
            raise RuntimeError(
                f"forgejo statuses request failed for {owner}/{repo} "
                f"({type(exc).__name__})"
            ) from None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError(
                f"forgejo statuses response was not JSON for {owner}/{repo}"
            ) from None

    def _read_capped(self, response: Any, owner: str, repo: str) -> bytes:
        """Read at most ``max_bytes``, in chunks; anything bigger raises."""
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > self._max_bytes:
                raise RuntimeError(
                    f"forgejo statuses response too large for {owner}/{repo}"
                )
            chunks.append(chunk)
        return b"".join(chunks)
