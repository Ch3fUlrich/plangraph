"""Free-text scrubbing, record-state derivation and record storage for
session records (``_StoreMixin`` carries Sessions' audit wrapper and the
on-disk record paths/writes).

Moved verbatim from sessions.py (SESSION-SPAWN-MCP split 3a/3b, 400-line
file gate); plangraph.spawner.sessions re-exports every name defined here.
"""

import json
import os
import subprocess
import sys
import threading

from plangraph.routing_errors import ParameterRefused
from plangraph.routing_support.private_io import atomic_write_private, append_private


_SCRUB_KEYS = frozenset({"reason", "error", "warning", "card", "message", "argv"})

# Per-name action locks (created lazily under the guard): callers come in
# SESSION-SPAWN-MCP slice 4.
_NAME_LOCKS = {}
_NAME_LOCKS_GUARD = threading.Lock()

# Per-thread `_audit_argv` carrier (D-3193): each MCP action runs in its own
# worker thread (asyncio.to_thread), so thread-local is per action.
_AUDIT_TLS = threading.local()


def _scrub_free(value):
    """Redact at every depth: this subtree is free text."""
    from plangraph.spawner.env_redact import redact_env_values
    if isinstance(value, str):
        return redact_env_values(value, os.environ)
    if isinstance(value, dict):
        return {k: _scrub_free(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_free(v) for v in value]
    return value


def _scrub_keep(value):
    """Structured context: scalars pass through untouched, containers are
    recursed so nested free-text fields are still found."""
    if isinstance(value, dict):
        return {k: (_scrub_free(v) if k in _SCRUB_KEYS else _scrub_keep(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_keep(v) for v in value]
    return value


def _scrub(value):
    """Deep scrub through the env redactor - free-text fields only (R-23).
    reason, error, warning, card and message texts and argv lists are
    redacted; structured fields - name, cwd, session_id and every path in a
    record - pass through untouched, since redacting those would corrupt ids
    and paths callers must act on. A bare string is free text by definition."""
    from plangraph.spawner.env_redact import redact_env_values
    if isinstance(value, str):
        return redact_env_values(value, os.environ)
    if isinstance(value, dict):
        return _scrub_keep(value)
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value

def _state(rec):
    """Record state for launch gating and list (D-3193). stop() writes only
    the stopped_ts timestamp, so a set stopped_ts means "stopped" whatever the
    state field says; records predating the write-ahead launch record carry no
    state field and derive it the same way."""
    if rec.get("stopped_ts") is not None:
        return "stopped"
    state = rec.get("state")
    return state if state in ("launching", "running", "unknown", "stopped") else "running"


class _StoreMixin:
    """Audit wrapper and on-disk record storage for ``Sessions``.

    Method bodies moved verbatim from ``Sessions`` (SESSION-SPAWN-MCP split
    3b, 400-line file gate); they resolve their module globals here.
    """

    # ---- one audit wrapper over every action (R-5) ---------------------

    @staticmethod
    def _caller():
        """Audit caller: the operating principal ($PLANGRAPH_PRINCIPAL) or the
        OS user - never a source location (R-15)."""
        import getpass
        return os.environ.get("PLANGRAPH_PRINCIPAL") or getpass.getuser()

    def _audit(self, action, name, ok, reason, session_id, argv):
        entry = {"ts": self._now(), "action": action, "name": str(name)[:80],
                 "session_id": session_id, "ok": bool(ok),
                 "reason": _scrub(reason) if reason else None, "caller": self._caller()}
        if argv is not None:
            entry["argv"] = list(argv)
        append_private(self._sessions / "audit.log", json.dumps(_scrub(entry)) + "\n")

    def _audit_guarded(self, action, name, ok, reason, session_id, argv, outcome):
        """One guarded audit write: a failing audit write must never replace
        the action's own outcome - the outcome (result dict or the exception
        about to be raised) carries a warning field instead (R-22)."""
        try:
            self._audit(action, name, ok, reason, session_id, argv)
        except (OSError, ValueError) as exc:
            note = f"audit write failed: {type(exc).__name__}: {_scrub(str(exc))}"
            if isinstance(outcome, dict):
                outcome["warning"] = f"{outcome['warning']}; {note}" if outcome.get("warning") else note
            else:
                outcome.warning = note

    @property
    def _audit_argv(self):
        """Prompt-redacted argv for the action in flight, thread-local so
        concurrent actions on one instance cannot audit each other's argv."""
        return getattr(_AUDIT_TLS, "argv", None)

    @_audit_argv.setter
    def _audit_argv(self, value):
        _AUDIT_TLS.argv = value

    def _action(self, action, name, fn, *, dry=False):
        """Run one action; every outcome - success, refusal, timeout, OSError,
        subprocess failure - leaves exactly one scrubbed audit JSON line (no
        line at all on a dry run, which stays side-effect free, D-2918).
        `self._audit_argv` is the prompt-redacted argv once one is built, so
        failure audit lines carry it too (R-16)."""
        self._audit_argv = None
        try:
            result = fn()
        except ParameterRefused as exc:
            if not dry:
                self._audit_guarded(action, name, False, str(exc), None, self._audit_argv, exc)
            raise
        except subprocess.TimeoutExpired as exc:
            outcome = subprocess.TimeoutExpired(_scrub(exc.cmd), exc.timeout)
            if not dry:
                self._audit_guarded(action, name, False, f"timeout after {exc.timeout}s",
                                    None, self._audit_argv, outcome)
            raise outcome from None
        except _ActionFailed as exc:
            reason = str(exc)
            outcome = {"ok": False, "name": name,
                       "session_id": getattr(exc, "session_id", None),
                       "error": reason}
            if not dry:
                self._audit_guarded(action, name, False, reason,
                                    outcome["session_id"], self._audit_argv, outcome)
            return _scrub(outcome)
        except (OSError, subprocess.SubprocessError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            outcome = {"ok": False, "name": name,
                       "session_id": getattr(exc, "session_id", None),
                       "error": reason}
            if not dry:
                self._audit_guarded(action, name, False, reason,
                                    outcome["session_id"], self._audit_argv, outcome)
            return _scrub(outcome)
        except Exception as exc:
            # Any other unexpected exception is an internal fault: it is
            # audited with its type and scrubbed message and surfaced as a
            # refusal carrying that same text, never as a raw traceback (R-22).
            text = f"{type(exc).__name__}: {_scrub(str(exc))}"
            outcome = ParameterRefused(text)
            if not dry:
                self._audit_guarded(action, name, False, text, None, self._audit_argv, outcome)
            raise outcome from None
        if dry:
            return _scrub(result)
        argv = result.pop("_audit_argv", None)
        self._audit_guarded(action, name, True, None, result.get("session_id"), argv, result)
        return _scrub(result)

    def audit_refusal(self, action, name, reason):
        """Public entry for refusals raised at the MCP boundary, before any
        _action runs (D-3193 point 6): the same single scrubbed audit line,
        best effort - the caller's ParameterRefused is the real outcome."""
        self._audit_guarded(action, name, False, reason, None, None,
                            ParameterRefused(reason))

    # ---- storage -------------------------------------------------------

    def _record_path(self, name):
        return self._sessions / f"{name}.json"

    def _marker_path(self, name):
        return self._sessions / f"{name}.ready.json"

    def _all_records(self):
        out = []
        try:
            paths = sorted(self._sessions.glob("*.json"))
        except OSError:
            return out
        for path in paths:
            if path.name.endswith(".ready.json"):
                continue
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def _name_lock(self, name):
        """The threading.Lock serializing actions on this name (created under
        the module guard; stable per name). Names differing only in case
        share one lock (their record paths collide on case-insensitive
        filesystems, Windows included)."""
        key = str(name).casefold()
        lock = _NAME_LOCKS.get(key)
        if lock is None:
            with _NAME_LOCKS_GUARD:
                lock = _NAME_LOCKS.setdefault(key, threading.Lock())
        return lock

    def _find_record(self, name, session_id):
        if name:
            try:
                rec = json.loads(self._record_path(name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            # Case-insensitive file systems resolve "Alpha.json" to alpha.json:
            # a record is returned only under the name it really carries.
            if isinstance(rec, dict) and rec.get("name") == name:
                return rec
            return None
        for rec in self._all_records():
            if session_id and rec.get("session_id") == session_id:
                return rec
        return None

    def _write_record(self, name, rec):
        """Record writes go to the VALIDATED name's path only - never to a
        path derived from record content (R-26)."""
        atomic_write_private(self._record_path(name), json.dumps(_scrub(rec)) + "\n")

    def _find_record_by_id(self, session_id):
        """(record, name) for a session id: the write-back name is the record
        file's stem, validated - never the record's own name field (R-26)."""
        for path in sorted(self._sessions.glob("*.json")):
            if path.name.endswith(".ready.json"):
                continue
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get("session_id") != session_id:
                continue
            if not NAME_RE.fullmatch(path.stem):
                raise _refuse(f"record file name is not a valid session name: {path.name}")
            return rec, path.stem
        return None, None


# NAME_RE, _refuse and _ActionFailed are imported LAST on purpose, mirroring
# the cross-import arrangement documented in sessions.py: sessions_config
# ends by binding _scrub from plangraph.spawner.sessions, and that module
# takes its _scrub from THIS module - so when this module is first imported
# FROM sessions.py, that module is still mid-import and has not bound _scrub
# yet. Bind it there first (the same function object); then every import
# order resolves.

_landing = sys.modules.get("plangraph.spawner.sessions")
if _landing is not None and getattr(_landing, "_scrub", None) is None:
    _landing._scrub = _scrub

from plangraph.spawner.sessions_agents import _ActionFailed
from plangraph.spawner.sessions_config import NAME_RE, _refuse
