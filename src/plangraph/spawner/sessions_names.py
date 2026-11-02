"""Session names (D-3605). Config ``session_names``: "role-counter" (default) or "free" (legacy).
Role-counter: launch(name=<role>) assigns ``<role>-NNNN`` = max(``_counters.json``, highest NNNN
of the role's records) + 1, written before the start (never reused); relaunch starts a NEW one.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

from plangraph.routing_support.private_io import atomic_write_private

_TAIL_RE = re.compile(r"(.+)-(\d+)\Z")
_LIVE = ("launching", "running", "unknown")
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


def _flock(fd):
    """Exclusive advisory lock on fd, released on close (fcntl.flock, else msvcrt)."""
    if fcntl is not None:
        return fcntl.flock(fd, fcntl.LOCK_EX)
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        try:
            return msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        except OSError:
            time.sleep(0.05)  # LK_LOCK gives up after ~10 s: keep waiting


class _NamesMixin:
    def _names_mode(self):
        try:
            return self._load_config().get("session_names", "role-counter")
        except Exception:  # the action re-reads the config and refuses it, audited
            return "free"

    def _role_of(self, name, strict):
        """(role, digits|None); strict (launch) refuses a -<digits> tail of other than 4 digits."""
        m = _TAIL_RE.fullmatch(name)
        if m is None or (not strict and len(m.group(2)) != 4):
            return name, None
        if len(m.group(2)) != 4:
            raise _refuse(f"name {name!r}: a numbered name is <role>-NNNN with exactly 4 digits")
        return m.group(1), m.group(2)

    @contextlib.contextmanager
    def _counter_flock(self):
        """Cross-process lock around read-next -> write counters; taken after the role lock."""
        if self._names_mode() != "role-counter":
            yield
            return
        self._sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._sessions / "_counters.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _flock(fd)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the lock

    def _lock_key(self, name):
        """Lock for launch, relaunch, stop, mark_ready: the role (role-counter mode) or the name."""
        if not isinstance(name, str) or self._names_mode() != "role-counter":
            return str(name)
        m = _TAIL_RE.fullmatch(name)
        return "role:" + (m.group(1) if m else name)  # ':' never occurs in a name

    def _counters(self):
        path = self._sessions / COUNTERS_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raw = None  # refused below
        if not isinstance(raw, dict) or not all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in raw.values()):
            raise _refuse(f"{COUNTERS_FILE} is unreadable or corrupt: {path}")
        return raw

    def _role_records(self, role):
        tail = re.compile(re.escape(role) + r"-(\d{4})", re.IGNORECASE)
        return [(int(m.group(1)), rec) for rec in self._all_records() if (m := tail.fullmatch(rec["name"]))]

    def _next_number(self, role):
        top = max([self._counters().get(role.casefold(), 0)] + [n for n, _ in self._role_records(role)])
        if top >= 9999:
            raise _refuse(f"role {role} has used all numbers up to 9999")
        return top + 1

    def _reserve_number(self, role, number):
        counters = {**self._counters(), role.casefold(): number}
        atomic_write_private(self._sessions / COUNTERS_FILE, json.dumps(counters, sort_keys=True) + "\n")

    def _launch_name(self, cfg, name):
        """Role-counter: the role's next name; an explicit <role>-NNNN must be exactly that."""
        if cfg.get("session_names", "role-counter") != "role-counter" or not isinstance(name, str):
            return name
        role, digits = self._role_of(name, strict=True)
        expected = f"{role}-{self._next_number(role):04d}"
        if digits is not None and name != expected:
            raise _refuse(f"session name {name} is not the next number for role {role}: expected {expected}")
        return expected

    def _reserve_counter(self, cfg, name):
        if cfg.get("session_names", "role-counter") == "role-counter":
            role, digits = self._role_of(name, strict=True)
            self._reserve_number(role, int(digits))  # called under the role lock

    def _relaunch_name(self, cfg, name):
        """The relaunched name (reserved here): same in free mode, else the next number; refused if superseded."""
        if cfg.get("session_names", "role-counter") != "role-counter":
            return name
        role, digits = self._role_of(name, strict=False)
        for n, rec in self._role_records(role) if digits else ():  # digit-less legacy names are never superseded
            if n > int(digits) and _state(rec) in _LIVE:
                raise _refuse(f"session {name} is superseded by {rec['name']}: relaunch {rec['name']}")
        number = self._next_number(role)  # the caller holds the role lock and flock
        self._reserve_number(role, number)
        return f"{role}-{number:04d}"

    def _relaunch_record(self, rec, name, handoff_path):
        """The relaunched record; a new name gets its own prompt copy and launch clock."""
        new_rec = {**rec, "handoff": str(handoff_path) if handoff_path else None}
        if name != rec.get("name"):
            prompt_path = self._sessions / f"{name}.prompt.txt"
            atomic_write_private(prompt_path, Path(rec["prompt_file"]).read_text(encoding="utf-8"))
            new_rec.update({"name": name, "prompt_file": str(prompt_path)})
            new_rec.pop("launched_ts", None)
        return new_rec


# Bound last: sessions_config imports sessions, which imports this module (class above first).
from plangraph.spawner.sessions_config import COUNTERS_FILE, _refuse
from plangraph.spawner.sessions_store import _state
