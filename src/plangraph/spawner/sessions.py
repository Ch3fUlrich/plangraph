"""Claude Code session launching for plangraph plan handoff (SESSION-SPAWN-MCP).

Launches background Claude Code agents that bootstrap a plan from a
``handoff.md`` produced by a writer. ``SessionManager._spawner_session`` and the
``spawner/mcp.py`` session tools drive the same ``Sessions`` class; config is
data loaded from a JSON file - no shell command string is ever built from it
(D-2873). Every outcome is one audited JSON line and nothing escapes untracked.

The config is the trust anchor (docs/SPAWNER.md, "Threat model"): on POSIX it
is opened once with ``O_NOFOLLOW`` and read from that fd, and must be a regular
file owned by the current user with mode 0600 or stricter. Other processes of
the same OS account are trusted - they can already run ``claude`` themselves.

Windows note: ``claude`` is an npm shim (``claude.cmd`` / ``claude.ps1``) on
Windows and a shim would let cmd.exe parse the prompt, so the Windows path
resolves ``claude`` to its real ``.exe`` (``shutil.which``) and refuses shims.
Windows uses the same detached ``claude --bg`` path (D-3193 probe); the cwd
must already be trusted in Claude Code.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from plangraph.routing_errors import ParameterRefused
from plangraph.routing_support.private_io import atomic_write_private, append_private

from .sessions_store import _SCRUB_KEYS, _scrub, _scrub_free, _scrub_keep, _state

__all__ = ["SESSION_CONFIG_KEYS", "Sessions", "transcript_path"]


# Config parsing and fail-closed argument checks live in sessions_config
# (SESSION-SPAWN-MCP slice 1/4). This import sits here - below _scrub,
# above class Sessions - on purpose: sessions_config ends by binding _scrub
# from this module, so both import orders resolve only in this arrangement.
from plangraph.spawner.sessions_config import (
    NAME_RE, SESSION_CONFIG_KEYS, _ConfigMixin, _refuse,
)

# Launch/resolve internals live in sessions_agents (SESSION-SPAWN-MCP slice
# 2/4) and are re-exported here because callers and tests take these names
# from this module; the import sits below the sessions_config import for the
# cross-import-order reason documented above.
from plangraph.spawner.sessions_agents import (
    DETACHED, FINISHED_STATES, LAUNCH_TIMEOUT, SHORT_RE, STOP_TIMEOUT,
    TAIL_BYTES, USAGE_KEYS, UUID_RE, _ActionFailed, _AgentsMixin,
    transcript_path,
)


class Sessions(_ConfigMixin, _AgentsMixin):
    """Launch / stop / relaunch Claude Code sessions.

    One injectable runner carries every subprocess call (argv list, never a
    shell string). Constructor: ``state_root`` (session state directory),
    ``config_path`` (else $PLANGRAPH_SESSIONS_CONFIG), ``run``
    (injectable), ``now`` (clock), ``platform``, ``resolve_budget`` (seconds to
    keep polling ``claude agents`` for the new session's row, 0.5 s apart),
    ``which`` (exe resolver, consulted on win32 only), ``sleep`` (poll wait).
    """

    def __init__(self, state_root=None, *, config_path=None, run=None,
                 now=None, platform=None, resolve_budget=15.0, which=None, sleep=None):
        self._run = run if run is not None else subprocess.run
        self._now = now if now is not None else time.time
        self._platform = platform if platform is not None else sys.platform
        self._which = which if which is not None else shutil.which
        self._sleep = sleep if sleep is not None else time.sleep
        self._resolve_budget = float(resolve_budget)
        self._config_path = config_path or os.environ.get("PLANGRAPH_SESSIONS_CONFIG")
        if state_root is None:
            state_root = os.environ.get("PLANGRAPH_SPAWN_STATE") or str(
                Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "plangraph")
        self._state = Path(state_root)
        self._sessions = self._state / "sessions"

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
            self._audit_guarded(action, name, False, f"timeout after {exc.timeout}s",
                                None, self._audit_argv, outcome)
            raise outcome from None
        except _ActionFailed as exc:
            outcome = {"ok": False, "name": name, "session_id": None}
            self._audit_guarded(action, name, False, str(exc), None, self._audit_argv, outcome)
            return _scrub(outcome)
        except (OSError, subprocess.SubprocessError) as exc:
            outcome = {"ok": False, "name": name, "session_id": None}
            self._audit_guarded(action, name, False, f"{type(exc).__name__}: {exc}",
                                None, self._audit_argv, outcome)
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

    def _find_record(self, name, session_id):
        if name:
            try:
                rec = json.loads(self._record_path(name).read_text(encoding="utf-8"))
                return rec if isinstance(rec, dict) else None
            except (OSError, ValueError):
                return None
        for rec in self._all_records():
            if session_id and rec.get("session_id") == session_id:
                return rec
        return None

    def _write_record(self, name, rec):
        """Record writes go to the VALIDATED name's path only - never to a
        path derived from record content (R-26)."""
        atomic_write_private(self._record_path(name), json.dumps(_scrub(rec)) + "\n")

    def _start(self, name, rec, argv, env, cwd_path, prompt_arg, exclude=None):
        """Write-ahead launch (D-3193): the record is written with state
        "launching", the redacted argv and the pre-start launched_ms BEFORE the
        process starts, so every later outcome is a state update on a record
        that already exists. rc 0 + id -> "running" (+ session_id, job_id);
        rc 0 + no id -> "unknown", ok result with warning; rc != 0 ->
        "stopped" + stopped_ts and _ActionFailed; TimeoutExpired / OSError /
        ParameterRefused from the run or the id poll -> "unknown" written and
        the original exception re-raised. The marker is unlinked only after
        the "running"/"unknown" write. Returns (session_id, warning)."""
        launched_ms = self._now() * 1000.0  # before the start: rows near it count
        rec.update({"state": "launching", "session_id": None, "job_id": None,
                    "stopped_ts": None, "ts": self._now(),
                    "launched_ms": launched_ms, "argv": self._shown(argv, prompt_arg)})
        # The write-ahead record must land before the process starts: if it
        # cannot, no process is started at all.
        self._write_record(name, rec)
        try:
            sid, warning = self._run_launch(name, argv, env, cwd_path, prompt_arg,
                                            exclude=exclude, launched_ms=launched_ms)
        except _ActionFailed:
            # claude exited non-zero: nothing is live, the run failed outright.
            rec.update({"state": "stopped", "stopped_ts": self._now()})
            try:
                self._write_record(name, rec)
            except (OSError, ValueError):
                pass  # the launch failure stays the outcome
            raise
        except (subprocess.TimeoutExpired, OSError, ParameterRefused):
            # The process may have started: state "unknown" records the
            # possible orphan (session_id null, stopped_ts unset) so list
            # shows it and relaunch can replace it. The write is best
            # effort: the launch failure stays the outcome.
            rec["state"] = "unknown"
            try:
                self._write_record(name, rec)
            except (OSError, ValueError):
                pass
            raise
        rec.update({"state": "running" if sid else "unknown",
                    "session_id": sid, "job_id": sid[:8] if sid else None,
                    "ts": self._now()})
        try:
            self._write_record(name, rec)
            self._marker_path(name).unlink(missing_ok=True)  # a stale marker never outlives a launch
        except (OSError, ValueError) as exc:
            # The session is running: its id must survive the failure or the
            # new session is untracked (round 4).
            raise _ActionFailed(f"session started (id {sid}) but record write failed: {exc}") from exc
        return sid, warning

    # ---- actions -------------------------------------------------------

    def launch(self, *, name, cwd, model, effort, permission_mode, mcp_config,
               prompt=None, prompt_file=None, handoff=None, parent=None, dry_run=False):
        return self._action("launch", name, lambda: self._launch_inner(
            name, cwd, model, effort, permission_mode, mcp_config,
            prompt, prompt_file, handoff, parent, dry_run), dry=dry_run is True)

    def _launch_inner(self, name, cwd, model, effort, permission_mode, mcp_config,
                      prompt, prompt_file, handoff, parent, dry_run):
        if not isinstance(dry_run, bool):
            raise _refuse("dry_run must be a real boolean")
        if parent is not None and (not isinstance(parent, str) or not parent):
            raise _refuse("parent must be a non-empty string")
        cfg = self._load_config()
        name = self._check_name(name)
        cwd_path = self._check_cwd(cwd, cfg)
        model = self._check_choice(model, cfg.get("models"), "model")
        effort = self._check_choice(effort, cfg.get("efforts"), "effort")
        mode = self._check_choice(permission_mode, cfg.get("permission_modes"), "permission_mode")
        mcp_path = self._check_mcp(mcp_config, cfg)
        text = self._prompt_text(prompt, prompt_file, cfg)
        handoff_path = self._handoff_arg(handoff, cfg) if handoff is not None else None
        prompt_arg = (f"Restart from handoff {handoff_path}.\n\n" + text) if handoff_path else text
        style = self._style_text(cfg)
        bg_argv, env = self._build_argv(
            name, cwd_path, model, effort, mode, mcp_path, style, prompt_arg, cfg)
        self._audit_argv = self._shown(bg_argv, prompt_arg)
        existing = self._find_record(name, None)
        if existing is not None:
            state = _state(existing)
            if state in ("launching", "running", "unknown"):
                # A live - or possibly live ("unknown": the process may have
                # started) - record is only replaced through relaunch (R-19).
                raise _refuse(f"session {name} has a {state} record; stop or relaunch it")
            if existing.get("session_id"):
                # A stopped record's old id may have outlived its stop: a listed id
                # outside a finished state means the old session is still running
                # and a launch here would start a second one. Checked in dry_run
                # too - it only reads (round 4).
                old = existing.get("session_id")
                row = next((r for r in self._agents_rows_strict()
                            if r.get("sessionId") == old), None)
                if row is not None and str(row.get("state") or "") not in FINISHED_STATES:
                    raise _refuse(f"session {name} old id {old} still listed "
                                  f"({row.get('state')}); use relaunch")
        if dry_run:
            # D-2918: a refusing dry run is still dry; a passing one writes
            # nothing either - planned argv, cwd and env keys only.
            return {"argv": self._shown(bg_argv, prompt_arg), "cwd": str(cwd_path),
                    "env_keys": sorted(k for k in ("FLEET_SESSION_NAME", "CLAUDE_CODE_AUTO_COMPACT_WINDOW"))}
        prompt_path = self._sessions / f"{name}.prompt.txt"
        atomic_write_private(prompt_path, text)  # before the session starts (R-19)
        rec = {"name": name, "cwd": str(cwd_path), "model": model, "effort": effort,
               "mcp": str(mcp_path), "prompt_file": str(prompt_path),
               "handoff": str(handoff_path) if handoff_path else None,
               "parent": parent, "children": [], "ts": None,
               "session_id": None, "job_id": None,
               "permission_mode": mode, "platform": self._platform,
               "stopped_ts": None}
        sid, warning = self._start(name, rec, bg_argv, env, cwd_path, prompt_arg)
        result = {"ok": True, "name": name, "session_id": sid,
                  "_audit_argv": self._shown(bg_argv, prompt_arg)}
        if warning:
            result["warning"] = warning
        return result

    def stop(self, name=None, session_id=None):
        return self._action("stop", name or session_id or "", lambda: self._stop_inner(name, session_id))

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

    def _stop_inner(self, name, session_id):
        if not name and not session_id:
            raise _refuse("stop needs a name or id")
        if name:
            name = self._check_name(name)
            rec = self._find_record(name, session_id)
            if rec is not None and session_id and rec.get("session_id") != session_id:
                raise _refuse(f"session_id does not match session {name}")
        else:
            rec, name = self._find_record_by_id(session_id)
        if rec is None:
            raise _refuse(f"unknown session: {name or session_id}")
        if _state(rec) == "stopped":
            # Nothing can be live behind a stopped record: ok without any
            # subprocess. A record with no session id is NOT refused here -
            # _stop_record resolves or clears it (D-3193).
            return {"ok": True, "name": name, "session_id": rec.get("session_id")}
        sid = self._stop_record(rec, name)
        return {"ok": True, "name": name, "session_id": sid}

    def _stop_record(self, rec, name):
        """claude stop <id> - the one and only stop mechanism, never rm/kill.
        The argv head is the resolved _claude_head() like every other call.

        A record without a session id gets one resolved first from the strict
        `claude agents` poll: the newest background row of this name whose
        startedAt is within 5 s of the record's own launch (launched_ms) is the
        session to stop; no such row means nothing is left to stop and the
        record is marked stopped outright. After a zero exit the strict poll is
        the CONFIRMATION - the id's row must be absent or in a finished state -
        and only then is the record marked stopped (D-3193). An unconfirmed
        stop refuses and writes nothing; a failing poll propagates as
        _ActionFailed and the record is not marked stopped.

        The stopped fields are written as a merged copy, never onto `rec`:
        relaunch reuses its rec as the base of the new record and must not
        inherit the stop's state. Returns the stopped session id (None when
        there was nothing to stop)."""
        sid = rec.get("session_id")
        if not sid:
            cutoff = float(rec.get("launched_ms") or 0) - 5000.0
            best = None
            for row in self._agents_rows_strict():
                if row.get("name") != name or row.get("kind") != "background":
                    continue
                row_sid = row.get("sessionId")
                if not row_sid:
                    continue
                started = row.get("startedAt") or 0
                if started < cutoff:
                    continue
                if best is None or started >= (best.get("startedAt") or 0):
                    best = row
            sid = (best or {}).get("sessionId")
            if not sid:
                # No row, nothing to stop: the record ends here.
                self._write_record(name, {**rec, "state": "stopped",
                                          "stopped_ts": self._now()})
                return None
        proc = self._run(self._claude_head() + ["stop", sid],
                         capture_output=True, text=True, timeout=STOP_TIMEOUT)
        if proc.returncode != 0:
            raise _refuse(f"stop failed: claude stop exited {proc.returncode}")
        row = next((r for r in self._agents_rows_strict()
                    if r.get("sessionId") == sid), None)
        if row is not None and str(row.get("state") or "") not in FINISHED_STATES:
            raise _refuse(f"stop not confirmed: {sid} still {row.get('state')}")
        self._write_record(name, {**rec, "state": "stopped",
                                  "stopped_ts": self._now(), "session_id": sid})
        return sid

    def mark_ready(self, name, handoff, card=None):
        return self._action("mark_ready", name, lambda: self._mark_ready_inner(name, handoff, card))

    def _mark_ready_inner(self, name, handoff, card):
        if card is not None and not isinstance(card, str):
            raise _refuse("card must be a string")
        cfg = self._load_config()
        # The handoff is checked first: a bad handoff is the caller's error to
        # see, whatever the session state is.
        handoff_path = self._handoff_arg(handoff, cfg)
        self._check_name(name)
        if self._find_record(name, None) is None:
            raise _refuse(f"unknown session: {name}")
        marker = {"handoff": str(handoff_path), "card": card, "ts": self._now()}
        atomic_write_private(self._marker_path(name), json.dumps(_scrub(marker)) + "\n")
        return {"ok": True, "name": name, "handoff": str(handoff_path)}

    def relaunch(self, name, handoff=None):
        return self._action("relaunch", name, lambda: self._relaunch_inner(name, handoff))

    def _relaunch_handoff(self, rec, cfg, name, handoff):
        """Effective relaunch handoff: the argument wins, else the ready marker.
        With neither, the relaunch is refused ("ready marker or handoff
        required"). Freshness (handoff newer than the record), existence and
        containment always apply - the record's old handoff is never reused."""
        if handoff is not None:
            path = self._handoff_arg(handoff, cfg)
        else:
            marker_path = self._marker_path(name)
            if not marker_path.is_file():
                raise _refuse("ready marker or handoff required")
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                marker = {}
            raw = marker.get("handoff")
            if not isinstance(raw, str) or not raw:
                raise _refuse("ready marker handoff must be a path string")
            path = self._handoff_arg(raw, cfg)
        if path.stat().st_mtime < float(rec.get("ts") or 0):
            raise _refuse(f"handoff is not newer than the session record: {path}")
        return path

    def _relaunch_inner(self, name, handoff):
        cfg = self._load_config()
        name = self._check_name(name)
        rec = self._find_record(name, None)
        if rec is None:
            raise _refuse(f"unknown session: {name}")
        old_sid = rec.get("session_id")
        handoff_path = self._relaunch_handoff(rec, cfg, name, handoff)
        # The stored launch spec is re-validated in full BEFORE the old session
        # is stopped: a relaunch must never cost the user their session.
        prompt_path = Path(rec.get("prompt_file") or "")
        if not str(rec.get("prompt_file") or "") or not prompt_path.is_file():
            raise _refuse(f"prompt_file is missing for session {name}: {rec.get('prompt_file')}")
        text = prompt_path.read_text(encoding="utf-8")
        if text.startswith("-"):
            raise _refuse("prompt must not start with '-'")
        cwd_path = self._check_cwd(rec.get("cwd"), cfg)
        model = self._check_choice(rec.get("model"), cfg.get("models"), "model")
        effort = self._check_choice(rec.get("effort"), cfg.get("efforts"), "effort")
        mode = self._check_choice(rec.get("permission_mode"), cfg.get("permission_modes"), "permission_mode")
        mcp_path = self._check_mcp(rec.get("mcp"), cfg)
        prompt_arg = (f"Restart from handoff {handoff_path}.\n\n" + text) if handoff_path else text
        style = self._style_text(cfg)
        bg_argv, env = self._build_argv(
            name, cwd_path, model, effort, mode, mcp_path, style, prompt_arg, cfg)
        self._audit_argv = self._shown(bg_argv, prompt_arg)
        # The old session is stopped BEFORE the relaunch (D-3193): anything not
        # recorded as stopped is stopped and confirmed by _stop_record (a null
        # id is resolved by name there); a stopped record's id may have
        # outlived its stop - `claude agents --json` is the source of truth and
        # a listed id outside a finished/stopped state is stopped first, a
        # failed stop refuses the relaunch and nothing is launched, while an
        # unlisted id is gone for good and proceeds untouched.
        if _state(rec) != "stopped":
            self._stop_record(rec, name)
        elif old_sid:
            row = next((r for r in self._agents_rows_strict()
                        if r.get("sessionId") == old_sid), None)
            if row is not None and str(row.get("state") or "") not in FINISHED_STATES:
                self._stop_record(rec, name)
        new_rec = dict(rec)
        new_rec["handoff"] = str(handoff_path) if handoff_path else None
        # The relaunch starts through _start (D-3193): the write-ahead record
        # and every outcome - running, unknown, stopped - are _start's to
        # write; a failure propagates with the old session already stopped.
        sid, warning = self._start(name, new_rec, bg_argv, env, cwd_path, prompt_arg,
                                   exclude=old_sid)
        # The ready marker is unlinked only after _start returned: a failed
        # relaunch keeps it so a plain retry can relaunch (R-21).
        try:
            self._marker_path(name).unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            raise _ActionFailed(f"session started (id {sid}) but record write failed: {exc}") from exc
        result = {"ok": True, "name": name, "session_id": sid,
                  "_audit_argv": self._shown(bg_argv, prompt_arg)}
        warnings = []
        if warning:
            warnings.append(warning)
        if warnings:
            result["warning"] = "; ".join(warnings)
        return result

    def list(self):
        return self._action("list", "", self._list_inner)

    def _list_inner(self):
        cfg = self._load_config()
        cap = cfg.get("ctx_cap_tokens") or 300000
        rows = self._agents_rows()
        sessions = []
        for rec in self._all_records():
            sid = rec.get("session_id")
            agent = next((r for r in rows if (sid and r.get("sessionId") == sid)
                          or (not sid and r.get("name") == rec.get("name"))), {})
            tokens = self._context_tokens(rec.get("cwd"), sid)
            sessions.append({
                "name": rec.get("name"), "id": sid, "cwd": rec.get("cwd"),
                "context_tokens": tokens, "context_pct": tokens / cap,
                "state": agent.get("state") or _state(rec),
                "ready": self._marker_path(rec.get("name")).is_file(),
            })
        return {"sessions": sessions}
