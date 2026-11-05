"""PG-CHANGELOG-GEN: the read verb ``changelog(since)`` (any authenticated principal).

Reads the whole event log (oldest first), the item titles and the decision texts
the log references, and hands them to :func:`changelog.build`.  Read-only; the
decisions file is optional (an unreadable file just leaves the ids bare).
"""
from __future__ import annotations

from . import changelog, context_sources, events

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
        adapter = getattr(self.ctx, "decisions", None)
        texts = context_sources.decision_texts(
            adapter, sorted(changelog.referenced_decisions(log)))
        head = log[-1]["seq"] if log else 0
        return changelog.build(log, since, titles, texts, head)
