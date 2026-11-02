"""Session names: ``<role>-NNNN`` counters and relaunch-as-new-session (D-3605).

``_NamesMixin`` is mixed into ``Sessions``. Config ``session_names`` is
"role-counter" (default) or "free" (legacy: the caller picks any name and a
relaunch reuses it). In role-counter mode launch(name=<role>) assigns
``<role>-NNNN`` (exactly 4 digits) from a per-host counter kept in
``counters.json`` next to the records: next = max(stored counter, highest
NNNN among that role's records) + 1, taken under the role lock and written
atomically before the session starts, so a number is never reused. A relaunch
stops the old session and starts a NEW one under the next number; the old
record stays (stopped, transcript readable).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from plangraph.routing_support.private_io import atomic_write_private

_TAIL_RE = re.compile(r"(.+)-(\d+)\Z")


class _NamesMixin:
    def _names_mode(self):
        try:
            return self._load_config().get("session_names", "role-counter")
        except Exception:  # the action re-reads the config and refuses it, audited
            return "free"

    def _role_of(self, name, strict):
        """(role, digits|None). strict (launch): a `-<digits>` tail must be
        exactly 4 digits. Lenient (relaunch): any other tail is part of the role."""
        m = _TAIL_RE.fullmatch(name)
        if m is None or (not strict and len(m.group(2)) != 4):
            return name, None
        if len(m.group(2)) != 4:
            raise _refuse(f"name {name!r} ends in -{m.group(2)}: a numbered name is "
                          f"<role>-NNNN with exactly 4 digits; pass the role only")
        return m.group(1), m.group(2)

    def _lock_key(self, name):
        """launch's lock: the role in role-counter mode (the counter is per
        role), the name itself in free mode."""
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
            raw = None
        if not isinstance(raw, dict) or not all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in raw.values()):
            raise _refuse(f"{COUNTERS_FILE} is unreadable or corrupt: {path}")
        return raw

    def _next_number(self, role):
        key = role.casefold()
        top = self._counters().get(key, 0)
        tail = re.compile(re.escape(key) + r"-(\d{4})\Z")
        for rec in self._all_records():
            m = tail.fullmatch(str(rec.get("name") or "").casefold())
            if m:
                top = max(top, int(m.group(1)))
        if top >= 9999:
            raise _refuse(f"role {role} has used all numbers up to 9999")
        return top + 1

    def _reserve_number(self, role, number):
        counters = self._counters()
        counters[role.casefold()] = number
        atomic_write_private(self._sessions / COUNTERS_FILE, json.dumps(counters, sort_keys=True) + "\n")

    def _launch_name(self, cfg, name):
        """The name a launch will use. Role-counter: a role gets the next
        number; an explicit <role>-NNNN must carry exactly that number."""
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
        """The name the relaunched session gets (reserved here, under the
        role lock): the same name in free mode, else the role's next number."""
        if cfg.get("session_names", "role-counter") != "role-counter":
            return name
        role, _ = self._role_of(name, strict=False)
        with self._name_lock("role:" + role):
            number = self._next_number(role)
            self._reserve_number(role, number)
        return f"{role}-{number:04d}"

    def _relaunch_record(self, rec, name, handoff_path):
        """The record the relaunched session starts from. A new name gets its
        own copy of the prompt file and a fresh launch clock."""
        new_rec = dict(rec)
        new_rec["handoff"] = str(handoff_path) if handoff_path else None
        if name != rec.get("name"):
            prompt_path = self._sessions / f"{name}.prompt.txt"
            atomic_write_private(prompt_path, Path(rec["prompt_file"]).read_text(encoding="utf-8"))
            new_rec.update({"name": name, "prompt_file": str(prompt_path)})
            new_rec.pop("launched_ts", None)
        return new_rec


# Bound last: sessions_config ends by importing sessions, which imports this
# module - the class above must already exist when that happens.
from plangraph.spawner.sessions_config import COUNTERS_FILE, _refuse
