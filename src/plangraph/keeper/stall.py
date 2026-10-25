"""Computed stall alarms for ``GET /v1/alarms`` — evaluated on every read.

Three kinds, shaped like ``projector_alarms`` rows but with ``id`` and
``workflow_id`` set to ``None`` (nothing to acknowledge):

* ``stall_no_event`` — an item in ``claimed`` whose newest ``events`` row (or,
  if none, its ``updated_at``/``created_at`` timestamp) is older than
  :data:`STALL_S`.
* ``merged_open`` — an open (not ``done``/``closed``) item whose key appears as
  a ``Plan-Item`` trailer on a commit of its project's configured repo main.
* ``review_round_cap`` -- an open item at >= 3 review rounds since its last
  replan (PG-EFFICIENCY-GATES, D-2739 §0.2e).

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
    if not stamp:
        return None
    try:
        when = parse_iso(stamp)
    except Exception:
        return None
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
    repos = getattr(git, "repos", None)
    return repos if isinstance(repos, dict) else {}


def _fetcher(git, entry):
    """The ``(repo, ref) -> [(sha, value)]`` lookup: an injected callable wins."""
    if callable(git) or callable(entry):
        return git if callable(git) else entry
    return cli_check.plan_item_trailers


#: ``git log`` runs per repo at most this often: GET /v1/alarms is polled, and
#: an uncached read would spawn git on every request.
_CACHE_TTL_S = 15.0
_cache: dict = {}
_UNSET = object()


def _trailers(git, entry, ref=_UNSET):
    try:
        if ref is not _UNSET:
            fetch, repo = git, entry
        else:
            repo, ref = cli_check._repo_path(entry)
            fetch = _fetcher(git, entry)
        cacheable = fetch is cli_check.plan_item_trailers and _CACHE_TTL_S > 0
        stamp = time.monotonic()
        if cacheable:
            hit = _cache.get((repo, ref))
            if hit is not None and stamp - hit[0] < _CACHE_TTL_S:
                return hit[1]
        try:
            result = fetch(repo, ref)
        except Exception:  # noqa: BLE001 - a failing git never fails the read
            result = []
        result = result if isinstance(result, list) else []
        if cacheable:
            _cache[(repo, ref)] = (stamp, result)
        return result
    except Exception:  # noqa: BLE001 - a malformed repos entry never raises
        return []


def round_cap_alarms(conn, open_keys, now):
    """``review_round_cap`` rows: open items at >= ROUND_CAP rounds since replan (D-2739 §0.2e)."""
    import json
    from ..gates.rounds import ROUND_CAP, count_rounds
    by_key = {}
    for row in conn.execute(
            "SELECT key, type, ts, payload FROM events WHERE type IN"
            " ('changes_requested', 'item_logged') ORDER BY seq"):
        if row["key"] not in open_keys:
            continue
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        by_key.setdefault(row["key"], []).append(
            {"type": row["type"], "ts": row["ts"], "payload": payload})
    rows = []
    for key, events in by_key.items():
        summary = count_rounds(events)
        if summary["since_replan"] >= ROUND_CAP:
            rows.append({"id": None, "key": key, "kind": "review_round_cap",
                         "workflow_id": None,
                         "detail": (f"{summary['since_replan']} review rounds since replan"
                                    f" (total {summary['rounds']}); writer/fixer spawns refused"
                                    " until a strong-model replan is logged"),
                         "raised_at": iso(now)})
    return rows


def stall_alarms(conn, now, git=None):
    """``stall_no_event`` + ``merged_open`` rows over *conn* at epoch *now*."""
    events_last = _events_last(conn)
    rows, open_keys = [], {}
    for item in conn.execute(
        "SELECT key, project, state, updated_at, created_at, "
        "COALESCE(updated_at, created_at) AS ref_stamp FROM items ORDER BY key"
    ):
        key, state = item["key"], item["state"]
        if state in _CLOSED:
            continue
        open_keys[key] = item["project"]
        if state != "claimed":
            continue
        last = events_last.get(key)
        ref_stamp = last if last is not None else item["ref_stamp"]
        age = _age_s(parse_iso, ref_stamp, now)
        if age is not None and age > STALL_S:
            rows.append({"id": None, "key": key, "kind": "stall_no_event",
                         "workflow_id": None,
                         "detail": f"claimed, no event for {age}s (last {int(age // 3600)}h)",
                         "raised_at": ref_stamp})
    rows.extend(round_cap_alarms(conn, open_keys, now))
    if open_keys:
        entries = ([(None, git)] if callable(git)
                   else list(_repos_of(git).items()))
        seen = set()
        for project, entry in entries:
            for sha, value in _trailers(git, entry):
                if (value in open_keys and (value, sha) not in seen
                        and project in (None, open_keys[value])):   # a repo only speaks for its own project
                    seen.add((value, sha))
                    rows.append({"id": None, "key": value, "kind": "merged_open",
                                 "workflow_id": None,
                                 "detail": f"merged commit {sha}",
                                 "raised_at": iso(now)})
    return sorted(rows, key=lambda r: (r["key"], r["raised_at"]))
