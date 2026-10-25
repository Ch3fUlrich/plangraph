"""Turn one keeper ``messages`` row into at most one session wake.

The wake is an event, never a timer: :meth:`WakeHook.run` long-polls the keeper
read ``GET /v1/messages?since=<cursor>&wait_ms=`` and delivers when it returns,
so a real report reaches the router inside the long-poll budget (the poll is
capped by the next due wake, both far below 60 s) while an idle fleet costs
nothing.  There is deliberately no polling interval, no background thread and no
systemd timer in this module; that is the defect the routing-side nudge timer
leaves behind.

A wake is a fixed-text synthetic message posted into the target session's
conversation -- for OpenCode ``POST /api/session/<id>/synthetic`` with
``{"text": ..., "resume": true}``, which is part of the conversation and not a
user prompt, so it produces no visible user bubble; Claude sessions use their
own configured synthetic/inbox path with the same body.  A wake only signals
work: consumers read the rows with their own cursors, and this hook never
carries a message body into a wake, a log line or its state file.

Interface (what other code, e.g. the fleet-msg client, calls): build a
:class:`WakeHook` from a recipient -> :class:`SessionTarget` map, a sink that
delivers one wake and an optional :class:`WakeState` file, then push rows with
:meth:`on_row` (one row) or :meth:`feed` (the ordered batch the API returns).
:meth:`tick` delivers deferred wakes, :meth:`run` does all of it behind one
long-poll.  Every public method returns outcomes and never raises.

Behaviour and the vectors each guarantee covers:

* **one event = one wake** -- a row at or below the persisted cursor is a
  duplicate; the cursor is saved after every public call, so a restart resumes
  exactly where it stopped and a repeated NOTIFY cannot wake twice.
* **lost cursor** -- a missing or corrupt state file replays the read from the
  start; the debounce collapses that backlog into at most two wakes per session
  (the first immediately, the rest as one coalesced follow-up) and no row is
  skipped, so a rebuild neither storms nor loses work.
* **unknown session** -- a recipient with no configured session, an unusable row
  and a sink 404 are refused and logged (bounded list), the cursor still
  advances, and nothing retries forever or crashes.
* **wake storm** -- only the first event inside ``debounce_s`` posts; the rest
  coalesce into one follow-up wake that is still delivered.
* **busy target** -- a ``WakeBusy`` (or any unexpected) delivery failure is
  retried after ``retry_s`` and never dropped: the obligation lives in the same
  file as the cursor, so even a restart keeps it.

Sink contract: raise :class:`WakeBusy` when the target cannot take the wake now
(retry later), :class:`WakeRefused` when it never can (log and drop); anything
else is treated as busy, so no transient failure can swallow a wake.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DEFAULT_DEBOUNCE_S", "DEFAULT_RETRY_S", "DEFAULT_TEXT",
           "POLL_WAIT_MAX_MS", "QUIET_SENDERS", "SessionTarget", "WakeBusy",
           "WakeHook", "WakeRefused", "WakeState", "PollFailed"]

#: The nudge text the routing-side timer used; a wake carries no message body.
DEFAULT_TEXT = "fleet-wake"
#: A second event inside this window coalesces instead of posting again.
DEFAULT_DEBOUNCE_S = 20.0
#: A failed delivery is attempted again after this long -- never dropped.
DEFAULT_RETRY_S = 10.0
#: The keeper refuses ``wait_ms`` above 30000, so the poll budget is capped.
POLL_WAIT_MAX_MS = 30000
#: The router authors commits and reports; its own rows must wake nothing.
QUIET_SENDERS = frozenset({"router"})
#: Infrastructure recipients that are never a session, so never a refusal.
NON_SESSION_RECIPIENTS = frozenset({"keeper", "relay"})
#: Refusal records are bounded: a stuck sender cannot grow memory without end.
MAX_REFUSED = 100

_LOGGER = logging.getLogger("plangraph.fleet_bus.wake")


class WakeBusy(Exception):
    """The target cannot take the wake now: retry later, never drop it."""


class WakeRefused(Exception):
    """The wake can never be delivered (unknown session): log and drop it."""


class PollFailed(Exception):
    """The messages read failed: `run` logs it and retries, never stops."""


def _log(message):
    """Default sink for refusals and deferred wakes; never prints a payload."""
    _LOGGER.warning(message)


def _short(value):
    """A bounded, single-line rendering safe for a log record."""
    text = " ".join(str(value).split())
    return text[:64] + ("..." if len(text) > 64 else "")


def _seconds(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


@dataclass(frozen=True)
class SessionTarget:
    """One wakeable session: where to post and which wire shape to use.

    ``base`` is the endpoint of the session service (no default: deployments
    configure it, tracked files hold no site address).  ``kind`` selects the
    path template in the sink, ``opencode`` out of the box.
    """

    session: str
    base: str
    kind: str = "opencode"


class WakeState:
    """Durable cursor plus per-session wake bookkeeping, in one small JSON file.

    Missing, unreadable or corrupt content is never fatal: the hook starts from
    the beginning of the read and the debounce bounds what that replays.  The
    file is rewritten atomically (temp file then replace) with LF endings.
    """

    def __init__(self, path=None, log=None):
        self.path = Path(path) if path is not None else None
        self.cursor = 0
        self.last = {}       # session -> epoch of the last wake (runtime only:
                             # debounce smooths a live burst, never persisted)
        self.pending = {}    # session -> {"count": int, "due": epoch second}
        self._log = log or _log
        self.load()

    def load(self):
        """Replace in-memory state from the file, or start clean and say why."""
        if self.path is None:
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeError) as exc:
            self._log(f"wake state unreadable ({type(exc).__name__}); "
                      "replaying from the start of the read")
            return
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            data = None
        if not self._accept(data):
            self._log("wake state corrupt; replaying from the start of the read")

    def _accept(self, data):
        """Install *data* only when every field is usable; else keep the reset."""
        if not isinstance(data, dict):
            return False
        cursor = data.get("cursor")
        pending = data.get("pending")
        if type(cursor) is not int or cursor < 0:
            return False
        if not isinstance(pending, dict):
            return False
        clean_pending = {}
        for name, entry in pending.items():
            count = entry.get("count") if isinstance(entry, dict) else None
            due = entry.get("due") if isinstance(entry, dict) else None
            if not isinstance(name, str) or type(count) is not int or count < 1:
                return False
            if not _seconds(due):
                return False
            clean_pending[name] = {"count": count, "due": float(due)}
        # ``last`` is deliberately not restored: it only smooths a live burst,
        # while the durable facts are the cursor and the undelivered wakes.
        self.cursor, self.pending = cursor, clean_pending
        return True

    def save(self):
        """Persist atomically; a failed write is logged, never raised."""
        if self.path is None:
            return
        payload = json.dumps({"cursor": self.cursor,
                              "pending": self.pending}, sort_keys=True,
                             separators=(",", ":"), ensure_ascii=True) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
            os.replace(tmp, self.path)
        except OSError as exc:
            self._log(f"wake state not persisted ({type(exc).__name__})")


class WakeHook:
    """The interface other code calls: message row in, at most one wake out."""

    def __init__(self, sessions, sink, state=None, *, clock=time.time,
                 text=DEFAULT_TEXT, debounce_s=DEFAULT_DEBOUNCE_S,
                 retry_s=DEFAULT_RETRY_S, log=None):
        if not isinstance(sessions, dict) or any(
                not isinstance(name, str) or not isinstance(target, SessionTarget)
                for name, target in sessions.items()):
            raise ValueError("sessions maps a recipient name to a SessionTarget")
        if not isinstance(text, str) or not text:
            raise ValueError("wake text must be a nonempty string")
        if type(debounce_s) not in (int, float) or debounce_s < 0:
            raise ValueError("debounce_s must be >= 0")
        if type(retry_s) not in (int, float) or retry_s <= 0:
            raise ValueError("retry_s must be > 0")
        self.sessions = dict(sessions)
        self.sink = sink
        self._log = log or _log
        self.state = state if state is not None else WakeState(log=self._log)
        self.clock = clock
        self.text = text
        self.debounce_s = float(debounce_s)
        self.retry_s = float(retry_s)
        self.refused = []
        self._dirty = False

    # ------------------------------------------------------------- the seam
    @property
    def cursor(self):
        """Last accounted message sequence: the ``since`` of the next read."""
        return self.state.cursor

    @property
    def since(self):
        return self.state.cursor

    def on_row(self, row):
        """Handle one message row and return its outcome; never raises."""
        now = self.clock()
        outcome = self._handle(row, now)
        self.tick(now)
        return outcome

    def feed(self, rows):
        """Handle an ordered batch (the shape the messages API returns).

        All rows share one clock reading, so a replayed backlog is debounced as
        a single burst, and the trailing :meth:`tick` delivers anything that was
        already due -- a caller that only pushes rows still gets its deferred
        wake.
        """
        now = self.clock()
        outcomes = [self._handle(row, now) for row in rows] if rows else []
        self.tick(now)
        return outcomes

    def tick(self, now=None):
        """Deliver every pending wake whose retry/debounce time has come."""
        now = self.clock() if now is None else now
        outcomes = []
        for name in sorted(self.state.pending):
            entry = self.state.pending.get(name)
            if entry is None:
                continue
            if name not in self.sessions:
                # Configuration moved on: the obligation can never be delivered.
                self.state.pending.pop(name, None)
                self._dirty = True
                self._refuse(None, name, "pending wake has no configured session")
                continue
            if now >= entry["due"]:
                outcomes.append(self._deliver(name, now, entry["count"]))
        self._save()
        return outcomes

    def poll_wait_ms(self, cap=POLL_WAIT_MAX_MS):
        """Budget for the next long-poll: the caller's cap, the keeper's cap and
        never past the next due wake -- that is how a busy target is retried
        without any timer of our own."""
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            raise ValueError("poll wait cap must be integer milliseconds") from None
        cap = max(0, min(cap, POLL_WAIT_MAX_MS))
        dues = [entry["due"] for entry in self.state.pending.values()]
        if not dues:
            return cap
        return max(0, min(cap, int((min(dues) - self.clock()) * 1000)))

    def run(self, poll, stop, wait_ms=POLL_WAIT_MAX_MS, error_backoff_s=1.0):
        """Long-poll ``poll(since, wait_ms)`` until *stop* is set.

        The poll is the event source: the keeper returns as soon as a row is
        committed and at the latest after the budget :meth:`poll_wait_ms`
        computed, so wakes ride the message event instead of a schedule.  A poll
        failure is logged with its exception type only and backed off through
        ``stop.wait`` -- the same duck-typed seam ``discovery.Registry.run``
        takes -- so the loop outlives keeper restarts.
        """
        while not stop.is_set():
            try:
                rows = poll(self.state.cursor, self.poll_wait_ms(wait_ms))
            except Exception as exc:
                self._log(f"wake poll failed ({type(exc).__name__})")
                stop.wait(error_backoff_s)
                continue
            if rows:
                self.feed(rows)
            else:
                self.tick()
        return None

    # -------------------------------------------------------------- internals
    def _handle(self, row, now):
        if not isinstance(row, dict):
            return self._refuse(None, None, "message row is not an object")
        seq = row.get("seq")
        if type(seq) is not int or seq < 0:
            # Without a sequence there is nothing to deduplicate against, so a
            # redelivery would wake twice: refuse instead of guessing.
            return self._refuse(None, row.get("to"), "row has no message sequence")
        if seq <= self.state.cursor:
            return "duplicate"
        sender = row.get("from")
        if isinstance(sender, str) and sender in QUIET_SENDERS:
            self._account(seq)
            return "ignored"
        to = row.get("to")
        if not isinstance(to, str) or to not in self.sessions:
            self._account(seq)
            if isinstance(to, str) and to in NON_SESSION_RECIPIENTS:
                return "ignored"
            return self._refuse(seq, to, "unknown session")
        self._account(seq)
        return self._request(to, now)

    def _account(self, seq):
        if seq > self.state.cursor:
            self.state.cursor = seq
            self._dirty = True

    def _request(self, name, now):
        pending = self.state.pending.get(name)
        if pending is not None:
            # The wake obligation already exists: fold this event into it.
            pending["count"] += 1
            self._dirty = True
            return "coalesced"
        last = self.state.last.get(name)
        if last is not None and now - last < self.debounce_s:
            self.state.pending[name] = {"count": 1, "due": last + self.debounce_s}
            self._dirty = True
            return "deferred"
        return self._deliver(name, now, 1)

    def _deliver(self, name, now, count):
        try:
            self.sink.wake(self.sessions[name], self.text)
        except WakeRefused as exc:
            self.state.pending.pop(name, None)
            self._dirty = True
            return self._refuse(None, name, f"wake refused: {_short(exc)}")
        except Exception as exc:  # busy or unknown: keep the obligation, retry
            self.state.pending[name] = {"count": count, "due": now + self.retry_s}
            self._dirty = True
            self._log(f"wake for {name} deferred ({type(exc).__name__}), "
                      f"retry in {self.retry_s:g}s")
            return "retry"
        self.state.last[name] = now
        self.state.pending.pop(name, None)
        self._dirty = True
        return "woken"

    def _refuse(self, seq, to, reason):
        where = "" if to is None else f" -> {_short(to)}"
        entry = (f"row {seq}{where}: {reason}" if seq is not None
                 else f"wake{where}: {reason}")
        self.refused.append(entry)
        if len(self.refused) > MAX_REFUSED:
            del self.refused[:len(self.refused) - MAX_REFUSED]
        self._log(entry)
        return "refused"

    def _save(self):
        if not self._dirty:
            return
        self._dirty = False
        self.state.save()
