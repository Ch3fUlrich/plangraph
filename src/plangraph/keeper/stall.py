"""Computed stall alarms for ``GET /v1/alarms`` — evaluated on every read.

Two kinds, shaped like ``projector_alarms`` rows but with ``id`` and
``workflow_id`` set to ``None`` (nothing to acknowledge):

* ``stall_no_event`` — an item in ``claimed`` whose newest ``events`` row is
  older than :data:`STALL_S` (an item with no events at all counts as stalled).
* ``merged_open`` — an open (not ``done``/``closed``) item whose key appears as
  a ``Plan-Item`` trailer on a commit of its project's configured repo main.

The git side is injectable via ``git``: a callable ``(repo, ref) ->
[(sha, value)]``, an object with a ``repos`` mapping (a config), or ``None``,
in which case the project's config repos are read through the same
``cli_check.plan_item_trailers`` helper ``pg check`` uses.  No repo configured,
an invalid ref, or any git failure simply yields no ``merged_open`` rows.
"""
from __future__ import annotations

import time

#: A claimed item with no event in this many seconds is considered stalled.
STALL_S = 2 * 3600.0
_CLOSED = ("done", "closed")

from . import cli_check
from .store_support import iso, parse_iso


def _age_s(parse_iso, stamp, moment):
    """Whole seconds from *stamp* to *moment*; ``None`` if unparsable."""
    when = parse_iso(stamp)
    return None if when is None else int(moment - when.timestamp())


def _events_last(conn):
    """``{key: newest events.ts}`` for every key that has events."""
    last = {}
    for row in conn.execute("SELECT key, MAX(ts) AS last_ts FROM events"
                            " WHERE key IS NOT NULL GROUP BY key"):
        last[row["key"]] = row["last_ts"]
    return last


def _repos_of(git):
    """Config-style ``git``: a repos mapping, or the ``repos`` of a config object."""
    if git is None or callable(git):
        return {}
    if isinstance(git, dict):
        return git
    return getattr(git, "repos", None) or {}


def _fetcher(git, entry):
    """The ``(repo, ref) -> [(sha, value)]`` lookup: an injected callable wins."""
    if callable(git) or callable(entry):
        return git if callable(git) else entry
    return cli_check.plan_item_trailers


#: ``git log`` runs per repo at most this often: GET /v1/alarms is polled, and
#: an uncached read would spawn git on every request.
_CACHE_TTL_S = 15.0
_cache: dict = {}


def _trailers(fetch, repo, ref):
    cacheable = fetch is cli_check.plan_item_trailers and _CACHE_TTL_S > 0
    stamp = time.monotonic()
    if cacheable:
        hit = _cache.get((repo, ref))
        if hit is not None and stamp - hit[0] < _CACHE_TTL_S:
            return hit[1]
    try:
        result = fetch(repo, ref)
    except Exception:  # noqa: BLE001 - a failing git never fails the read
        return []
    result = result if isinstance(result, list) else []
    if cacheable:
        _cache[(repo, ref)] = (stamp, result)
    return result


def stall_alarms(conn, now, git=None):
    """``stall_no_event`` + ``merged_open`` rows over *conn* at epoch *now*."""
    events_last = _events_last(conn)
    rows, open_keys = [], {}
    for item in conn.execute("SELECT key, project, state FROM items ORDER BY key"):
        key, state = item["key"], item["state"]
        if state in _CLOSED:
            continue
        open_keys[key] = item["project"]
        if state != "claimed":
            continue
        last = events_last.get(key)
        age = _age_s(parse_iso, last, now) if last else None
        if last is None or (age is not None and age > STALL_S):
            rows.append({"id": None, "key": key, "kind": "stall_no_event",
                         "workflow_id": None,
                         "detail": (f"claimed, no event for {age}s (last {int(age // 3600)}h)"
                                    if age is not None else "claimed, no events"),
                         "raised_at": last or iso(now)})
    if open_keys:
        entries = ([(None, git)] if callable(git)
                   else list(_repos_of(git).items()))
        seen = set()
        for project, entry in entries:
            repo, ref = cli_check._repo_path(entry)
            for sha, value in _trailers(_fetcher(git, entry), repo, ref):
                if (value in open_keys and (value, sha) not in seen
                        and project in (None, open_keys[value])):   # a repo only speaks for its own project
                    seen.add((value, sha))
                    rows.append({"id": None, "key": value, "kind": "merged_open",
                                 "workflow_id": None,
                                 "detail": f"merged commit {sha}",
                                 "raised_at": iso(now)})
    return sorted(rows, key=lambda r: (r["key"], r["raised_at"]))
