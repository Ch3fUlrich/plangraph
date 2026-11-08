"""Relaunch locking and stop confirmation for spawner sessions."""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager

from .sessions_agents import FINISHED_STATES
from .sessions_config import _refuse
from .sessions_store import _state


class _RelaunchGuardMixin:
    @contextmanager
    def _relaunch_guard(self, name):
        """Role-ready-marker lock, independent of successor names and processes."""
        import fcntl

        name = self._check_name(name)
        key = hashlib.sha256(self._lock_key(name).casefold().encode()).hexdigest()
        self._sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._sessions / f"_relaunch-{key}.lock",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _refuse(f"session {name} already relaunched from this handoff") from None
            yield
        finally:
            os.close(fd)  # Do not unlink: waiters must see the same inode.

    def _guarded_relaunch(self, name, handoff):
        with self._relaunch_guard(name):
            with self._name_lock(self._lock_key(name)), self._counter_flock():
                return self._relaunch_inner(name, handoff)

    def _ensure_stopped(self, rec, name):
        """The id behind a record, stopped and confirmed; returns the stopped
        or confirmed-gone id (None when there is none). Not-recorded-stopped
        goes through _stop_record; a stopped record's id may outlive its stop
        - `claude agents` is the truth (D-3193): a listed live id is stopped
        again, an unlisted or finished one is gone for good."""
        if _state(rec) != "stopped":
            return self._stop_record(rec, name)
        sid = rec.get("session_id")
        if sid:
            row = next((r for r in self._agents_rows_strict()
                        if r.get("sessionId") == sid), None)
            if row is not None and str(row.get("state") or "") not in FINISHED_STATES:
                return self._stop_record(rec, name)
        return sid
