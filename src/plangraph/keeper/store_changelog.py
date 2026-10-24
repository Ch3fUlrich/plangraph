"""PG-CHANGELOG-GEN: the read verb ``changelog(since)`` (any authenticated principal).

Reads the whole event log (oldest first), the item titles and the decision texts
the log references, and hands them to :func:`changelog.build`.  Read-only; the
decisions file is optional (an unreadable file just leaves the ids bare).
"""
from __future__ import annotations

from . import changelog, events

__all__ = ["ChangelogMixin"]


class ChangelogMixin:
    """``changelog`` for :class:`plangraph.keeper.store.Store`."""

    def changelog(self, principal, since):
        return self._run(principal, "changelog", None,
                         lambda now: self._changelog(since))

    def _changelog(self, since):
        changelog.parse_since(since)                       # refuse a bad `since` before any read
        log = events.read_events(self.conn)
        titles = {row["key"]: row["title"]
                  for row in self.conn.execute("SELECT key, title FROM items")}
        texts = {}
        adapter = getattr(self.ctx, "decisions", None)
        if adapter is not None and callable(getattr(adapter, "text", None)):
            for decision_id in sorted(changelog.referenced_decisions(log)):
                try:
                    text = adapter.text(decision_id)
                except Exception:  # noqa: BLE001 - an unreadable decisions file leaves ids bare
                    text = None
                if text:
                    texts[decision_id] = text
        head = log[-1]["seq"] if log else 0
        return changelog.build(log, since, titles, texts, head)
