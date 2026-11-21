"""PG-CHANGELOG-GEN: the read verb ``changelog(since)`` (any authenticated principal).

Reads the whole event log (oldest first), the item titles and the decision texts
the log references, and hands them to :func:`changelog.build`.  Read-only; the
decisions file is optional (an unreadable file just leaves the ids bare).
"""
from __future__ import annotations

import json

from . import changelog, context_sources, events, timing
from .errors import Refused

__all__ = ["ChangelogMixin"]


class ChangelogMixin:
    """``changelog`` for :class:`plangraph.keeper.store.Store`."""

    def changelog(self, principal, since):
        return self._run(principal, "changelog", None,
                         lambda now: self._changelog(since))

    def event_page(self, principal, since, limit):
        return self._run(principal, "events", None,
                         lambda now: scoped_page(self.conn, principal, since, limit),
                         success_message=None)

    @timing.measured("changelog_timing")
    def _changelog(self, since):
        changelog.parse_since(since)                       # refuse a bad `since` before any read
        log = events.read_events(self.conn)
        titles = {row["key"]: row["title"]
                  for row in self.conn.execute("SELECT key, title FROM items")}
        adapter = getattr(self.ctx, "decisions", None)
        texts = context_sources.decision_texts(
            adapter, sorted(changelog.referenced_decisions(log)))
        head = log[-1]["seq"] if log else 0
        return changelog.build(log, since, titles, texts, head)


def record_audience(conn, seq, key, kind, payload, recipient, prior_holders):
    """Capture recipients inside the event transaction, before a claim can end."""
    project = None
    names = {recipient, *prior_holders}
    if key is not None:
        item = conn.execute(
            "SELECT project, owner, claimed_by, delegate, agent_id FROM items WHERE key=?",
            (key,)).fetchone()
        if item is None:
            return
        project = item["project"]
        names.update(item[field] for field in ("owner", "claimed_by", "delegate", "agent_id"))
        if kind in ("claimed", "item_reopened", "legacy_state_set"):
            names.add(payload.get("claimed_by"))
        if kind == "delegated":
            names.add(payload.get("worker"))
    for name in sorted(n for n in names if n):
        conn.execute("INSERT INTO event_audience(seq,recipient,project) VALUES(?,?,?)",
                     (seq, name, project))


def scoped_page(conn, principal, since, limit):
    """Page over committed sequence numbers, showing only authorized events."""
    if not 0 <= since <= 2**63 - 1 or not 1 <= limit <= 500:
        raise Refused("validation", "events needs since in 0..2^63-1 and limit in 1..500")
    # Slice first to bound work even for a principal with very sparse history.
    # The payload remains NULL for hidden rows, but their seq advances the scan.
    projects = list(principal.projects)
    scope = "a.project IS NULL"
    if principal.role == "router":
        scope = "1=1"
        projects = []
    elif projects:
        scope += " OR a.project IN (" + ",".join("?" for _ in projects) + ")"
    page = conn.execute(
        "WITH page AS (SELECT seq FROM events WHERE seq>? ORDER BY seq LIMIT ?) "
        "SELECT page.seq,e.ts,e.actor,e.role,e.model,e.key,e.type,e.payload "
        "FROM page LEFT JOIN event_audience a ON a.seq=page.seq AND a.recipient=? "
        f"AND ({scope}) LEFT JOIN events e ON e.seq=a.seq ORDER BY page.seq",
        (since, limit + 1, principal.name, *projects)).fetchall()
    scanned = page[:limit]
    rows = []
    for row in scanned:
        if row["type"] is not None:
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            if event["type"] == "token_minted":
                event["payload"].pop("token_sha256", None)
            rows.append(event)
    return {"rows": rows, "next_cursor": scanned[-1]["seq"] if scanned else since,
            "more": len(page) > limit}
