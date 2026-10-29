"""Claude Code session launching for plangraph plan handoff (SESSION-SPAWN-MCP).

Launches background Claude Code agents that bootstrap a plan from a
``handoff.md`` produced by a writer. ``SessionManager._spawner_session`` and the
``spawner/mcp.py`` session tools drive the same ``Sessions`` class; config is
data loaded from a JSON file - no shell command string is ever built from it
(D-2873). Every outcome is one audited JSON line and nothing escapes untracked.

Windows note: ``claude`` is an npm shim (``claude.cmd`` / ``claude.ps1``) on
Windows and a shim would let cmd.exe parse the prompt, so the Windows path
resolves ``claude`` to its real ``.exe`` (``shutil.which``), refuses shims, and
either starts it detached (``--bg``) or hands it to Windows Terminal (``wt.exe``).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from plangraph.routing_errors import ParameterRefused
from plangraph.routing_support.private_io import atomic_write_private, append_private

__all__ = ["SESSION_CONFIG_KEYS", "Sessions", "transcript_path"]

SESSION_CONFIG_KEYS = frozenset({
    "cwd_roots", "file_roots", "models", "efforts", "permission_modes",
    "mcp_configs", "style_file", "compact_window", "ctx_cap_tokens",
    "windows_launcher",
})
LAUNCH_TIMEOUT = 60
STOP_TIMEOUT = 30
DETACHED = 0x8 | 0x200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SHORT_RE = re.compile(r"backgrounded\s*\W*([0-9a-fA-F]{8})\b")
USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _scrub(value):
    """Deep scrub through the env redactor. Records, audit lines, returned
    results and error text all pass through here (R-6)."""
    from plangraph.spawner.env_redact import redact_env_values
    if isinstance(value, str):
        return redact_env_values(value, os.environ)
    if isinstance(value, dict):
        return {_scrub(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _refuse(message):
    """Refusal with scrubbed text: error messages reach callers and logs."""
    return ParameterRefused(_scrub(str(message)))


def transcript_path(cwd, session_id):
    """Claude transcript file for one session: every non-alphanumeric cwd
    character maps to "-" (spelled out by the tests on purpose), the session id
    is the file stem."""
    safe = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / safe / f"{session_id}.jsonl"


class _ActionFailed(Exception):
    """Subprocess-level failure: audited, then reported as {"ok": False}."""


class Sessions:
    """Launch / stop / relaunch Claude Code sessions.

    One injectable runner carries every subprocess call (argv list, never a
    shell string). Constructor: ``state_root`` (session state directory),
    ``config_path`` (else $PLANGRAPH_SESSIONS_CONFIG), ``run`` / ``popen``
    (injectables), ``now`` (clock), ``platform``, ``resolve_budget`` (seconds a
    ``claude agents`` row may be old and still count as the new session),
    ``which`` (exe resolver, consulted on win32 only).
    """

    def __init__(self, state_root=None, *, config_path=None, run=None, popen=None,
                 now=None, platform=None, resolve_budget=15.0, which=None):
        self._run = run if run is not None else subprocess.run
        self._popen = popen if popen is not None else subprocess.Popen
        self._now = now if now is not None else time.time
        self._platform = platform if platform is not None else sys.platform
        self._which = which if which is not None else shutil.which
        self._resolve_budget = float(resolve_budget)
        self._config_path = config_path or os.environ.get("PLANGRAPH_SESSIONS_CONFIG")
        if state_root is None:
            state_root = os.environ.get("PLANGRAPH_SPAWN_STATE") or str(
                Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "plangraph")
        self._state = Path(state_root)
        self._sessions = self._state / "sessions"

    # ---- config: parsed, trusted, fail-closed (R-1, R-3) ---------------

    def _load_config(self):
        if not self._config_path:
            raise _refuse("sessions config missing: set PLANGRAPH_SESSIONS_CONFIG")
        path = Path(self._config_path)
        try:
            st = path.stat()
        except OSError:
            raise _refuse(f"sessions config not found: {path}")
        if self._platform != "win32":
            # POSIX trust: the config is only ours to obey if it is ours alone.
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                raise _refuse(f"sessions config not owned by the current user: {path}")
            if st.st_mode & 0o022:
                raise _refuse(f"sessions config is group/world-writable: {path}")
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise _refuse(f"sessions config is not valid JSON: {path}")
        if not isinstance(cfg, dict):
            raise _refuse(f"sessions config must be a JSON object: {path}")
        self._check_config(cfg)
        return cfg

    def _check_config(self, cfg):
        roots = self._root_list(cfg, "cwd_roots")
        if not roots:
            raise _refuse("config cwd_roots must be a non-empty list of directories")
        self._root_list(cfg, "file_roots")
        for key in ("models", "efforts", "permission_modes"):
            values = cfg.get(key)
            if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
                raise _refuse(f"config {key} must be a non-empty list of strings")
        entries = cfg.get("mcp_configs")
        if not isinstance(entries, list):
            raise _refuse("config mcp_configs must be a list of file paths")
        for raw in entries:
            if not isinstance(raw, str) or not Path(raw).expanduser().resolve().is_file():
                raise _refuse(f"config mcp_configs entry is not a regular file: {raw}")
        if cfg.get("style_file") is not None:
            style = cfg["style_file"]
            if not isinstance(style, str) or not Path(style).is_absolute() or not Path(style).is_file():
                raise _refuse(f"config style_file must be an absolute readable file: {style}")
        if cfg.get("compact_window") is not None:
            window = cfg["compact_window"]
            if not isinstance(window, str) or not window.isdigit():
                raise _refuse("config compact_window must be a digit string")
        if cfg.get("ctx_cap_tokens") is not None:
            cap = cfg["ctx_cap_tokens"]
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                raise _refuse("config ctx_cap_tokens must be a positive int")
        if cfg.get("windows_launcher") not in (None, "bg", "wt"):
            raise _refuse("config windows_launcher must be 'bg' or 'wt'")

    def _root_list(self, cfg, key):
        raw_list = cfg.get(key)
        if raw_list is None:
            return []
        if not isinstance(raw_list, list):
            raise _refuse(f"config {key} must be a list of directories")
        roots = []
        for raw in raw_list:
            if not isinstance(raw, str) or not raw or not Path(raw).expanduser().is_absolute():
                raise _refuse(f"config {key} entries must be absolute directory paths: {raw}")
            path = Path(raw).expanduser().resolve()
            # A filesystem root would allow any path; refuse it outright.
            if len(path.parts) <= 1 or path.parent == path:
                raise _refuse(f"config {key} must not be a filesystem root: {raw}")
            if not path.is_dir():
                raise _refuse(f"config {key} entry is not an existing directory: {raw}")
            roots.append(path)
        return roots

    def _file_roots(self, cfg):
        return self._root_list(cfg, "cwd_roots") + self._root_list(cfg, "file_roots")

    def _inside(self, path, roots):
        return any(path == root or root in path.parents for root in roots)

    def _file_arg(self, value, cfg, what):
        """prompt_file / handoff: inside the roots, a readable regular file."""
        if not isinstance(value, str) or not value:
            raise _refuse(f"{what} must be a non-empty path string")
        path = Path(value).expanduser().resolve()
        if not self._inside(path, self._file_roots(cfg)):
            raise _refuse(f"{what} is outside the configured roots: {value}")
        if not path.is_file():
            raise _refuse(f"{what} is not a regular file: {value}")
        if not os.access(path, os.R_OK):
            raise _refuse(f"{what} is not readable: {value}")
        return path

    def _check_name(self, name):
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise _refuse(f"name must match [a-z0-9][a-z0-9-]{{0,63}}: {name!r}")
        return name

    def _check_cwd(self, cwd, cfg):
        if not isinstance(cwd, str) or not cwd:
            raise _refuse("cwd must be a non-empty path string")
        path = Path(cwd).expanduser().resolve()
        if not self._inside(path, self._root_list(cfg, "cwd_roots")):
            raise _refuse(f"cwd outside cwd_roots: {cwd}")
        if not path.is_dir():
            raise _refuse(f"cwd does not exist or is not a directory: {cwd}")
        return path

    def _check_choice(self, value, allowed, what):
        if not isinstance(value, str) or value not in (allowed or ()):
            raise _refuse(f"{what} not allowed by config: {value!r}")
        return value

    def _check_mcp(self, mcp_config, cfg):
        if not isinstance(mcp_config, str) or not mcp_config:
            raise _refuse("mcp_config is required")
        path = Path(mcp_config).expanduser().resolve()
        listed = {str(Path(e).expanduser().resolve()) for e in cfg.get("mcp_configs") or ()}
        if str(path) not in listed:
            raise _refuse(f"mcp_config not listed in config mcp_configs: {mcp_config}")
        if not path.is_file():
            raise _refuse(f"mcp_config is not a regular file: {mcp_config}")
        return path

    def _prompt_text(self, prompt, prompt_file, cfg):
        if (prompt is None) == (prompt_file is None):
            raise _refuse("exactly one of prompt or prompt_file is required")
        if prompt is not None:
            if not isinstance(prompt, str):
                raise _refuse("prompt must be a string")
            text = prompt
        else:
            text = self._file_arg(prompt_file, cfg, "prompt_file").read_text(encoding="utf-8")
        if not text.strip():
            raise _refuse("prompt must not be blank")
        # Dash guard runs on the raw prompt text, before any handoff prefix.
        if text.startswith("-"):
            raise _refuse("prompt must not start with '-'")
        return text

    def _style_text(self, cfg):
        style = cfg.get("style_file")
        if not style:
            return None
        return Path(style).read_text(encoding="utf-8").strip()

    def _handoff_arg(self, handoff, cfg):
        if not isinstance(handoff, str) or not handoff:
            raise _refuse("handoff must be a non-empty path string")
        return self._file_arg(handoff, cfg, "handoff")

    # ---- one audit wrapper over every action (R-5) ---------------------

    @staticmethod
    def _caller():
        frame = sys._getframe(1)
        for _ in range(8):
            if frame is None or Path(frame.f_code.co_filename).name != "sessions.py":
                break
            frame = frame.f_back
        return f"{Path(frame.f_code.co_filename).name}:{frame.f_lineno}" if frame else "unknown"

    def _audit(self, action, name, ok, reason, session_id, argv):
        entry = {"ts": self._now(), "action": action, "name": _scrub(str(name))[:80],
                 "session_id": session_id, "ok": bool(ok),
                 "reason": _scrub(reason) if reason else None, "caller": self._caller()}
        if argv is not None:
            entry["argv"] = list(argv)
        append_private(self._sessions / "audit.log", json.dumps(_scrub(entry)) + "\n")

    def _action(self, action, name, fn, *, dry=False):
        """Run one action; every outcome - success, refusal, timeout, OSError,
        subprocess failure - leaves exactly one scrubbed audit JSON line (no
        line at all on a dry run, which stays side-effect free, D-2918)."""
        try:
            result = fn()
        except ParameterRefused as exc:
            if not dry:
                self._audit(action, name, False, str(exc), None, None)
            raise
        except subprocess.TimeoutExpired as exc:
            self._audit(action, name, False, f"timeout after {exc.timeout}s", None, None)
            raise subprocess.TimeoutExpired(_scrub(exc.cmd), exc.timeout) from None
        except _ActionFailed as exc:
            self._audit(action, name, False, str(exc), None, None)
            return _scrub({"ok": False, "name": name, "session_id": None})
        except (OSError, subprocess.SubprocessError) as exc:
            self._audit(action, name, False, f"{type(exc).__name__}: {exc}", None, None)
            return _scrub({"ok": False, "name": name, "session_id": None})
        if dry:
            return _scrub(result)
        argv = result.pop("_audit_argv", None)
        self._audit(action, name, True, None, result.get("session_id"), argv)
        return _scrub(result)

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

    def _write_record(self, rec):
        atomic_write_private(self._record_path(rec.get("name")), json.dumps(_scrub(rec)) + "\n")

    # ---- subprocess argv construction (D-2873) -------------------------

    def _claude_head(self):
        if self._platform != "win32":
            return ["claude"]
        # A .cmd/.bat shim would let cmd.exe parse the prompt: only the real
        # .exe is ever handed to CreateProcess or Windows Terminal (R-9).
        resolved = self._which("claude")
        if not resolved:
            raise _refuse("claude not found on PATH; refusing shim-less Windows launch")
        if not str(resolved).endswith(".exe"):
            raise _refuse(f"claude must resolve to an .exe, not a shim: {resolved}")
        return [str(resolved)]

    def _build_argv(self, name, cwd_path, model, effort, mode, mcp_path, style, prompt_arg, cfg):
        head = self._claude_head()
        tail = ["--strict-mcp-config", "--mcp-config", str(mcp_path),
                "--remote-control", name, "-n", name,
                "--model", model, "--effort", effort, "--permission-mode", mode]
        if style:
            tail += ["--append-system-prompt", style]
        tail += [prompt_arg]
        bg_argv = head + ["--bg"] + tail
        wt_argv = ["wt.exe", "-w", "0", "new-tab", "--title", name,
                   "-d", str(cwd_path)] + head + tail
        cap = cfg.get("ctx_cap_tokens") or 200000
        compact = cfg.get("compact_window") or str(int(round(float(cap) * 1.1)))
        env = {**os.environ, "FLEET_SESSION_NAME": name,
               "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(compact)}
        launcher = "wt" if (self._platform == "win32" and cfg.get("windows_launcher") == "wt") else "bg"
        return bg_argv, wt_argv, env, launcher

    def _launch_run_kw(self, env):
        kw = {"capture_output": True, "text": True, "env": env, "timeout": LAUNCH_TIMEOUT}
        if self._platform == "win32":
            kw["creationflags"] = DETACHED
        return kw

    @staticmethod
    def _shown(argv, prompt_arg):
        shown = list(argv)
        shown[-1] = f"<{len(prompt_arg)} chars>"
        return shown

    def _agents_rows(self):
        try:
            proc = self._run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=STOP_TIMEOUT)
            rows = json.loads(proc.stdout or "[]")
            return rows if isinstance(rows, list) else []
        except (OSError, subprocess.SubprocessError, ValueError):
            return []

    def _resolve_id(self, name, stdout, exclude=None):
        """Map a just-launched session to its real session id. A full UUID in
        the launcher's own stdout wins outright; otherwise `claude agents`
        resolves the short id, falling back to name + startedAt (R-13). The
        previous session id is excluded from every agents lookup so a relaunch
        can never resolve to the session it replaced."""
        found = UUID_RE.search(stdout or "")
        if found:
            return found.group(0)
        rows = self._agents_rows()
        short = SHORT_RE.search(stdout or "")
        if short:
            for row in rows:
                if str(row.get("id") or "") == short.group(1) and row.get("sessionId") != exclude:
                    return row.get("sessionId")
        cutoff = self._now() * 1000.0 - self._resolve_budget * 1000.0
        best = None
        for row in rows:
            if row.get("name") != name or row.get("kind") != "background":
                continue
            sid = row.get("sessionId")
            if not sid or sid == exclude:
                continue
            started = row.get("startedAt") or 0
            if started < cutoff:
                continue
            if best is None or started >= (best.get("startedAt") or 0):
                best = row
        return best.get("sessionId") if best else None

    def _run_launch(self, name, bg_argv, wt_argv, env, launcher, cwd_path, prompt_arg, exclude=None):
        """Start the session; returns (session_id, warning). Raises _ActionFailed
        on a non-zero exit and TimeoutExpired / OSError upward to the wrapper.
        `exclude` is the previous session id on a relaunch: it must never be
        resolved as the new session's id (R-13)."""
        if launcher == "wt":
            kw = {k: v for k, v in self._launch_run_kw(env).items() if k not in ("capture_output", "text")}
            self._popen(wt_argv, cwd=str(cwd_path), **kw)
            # wt only exposes name + startedAt; the launcher's stdout is ignored.
            sid = self._resolve_id(name, "", exclude)
        else:
            proc = self._run(bg_argv, cwd=str(cwd_path), **self._launch_run_kw(env))
            if proc.returncode != 0:
                raise _ActionFailed(f"claude exited {proc.returncode}")
            sid = self._resolve_id(name, proc.stdout or "", exclude)
        return sid, (None if sid else "session id could not be resolved")

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
        bg_argv, wt_argv, env, launcher = self._build_argv(
            name, cwd_path, model, effort, mode, mcp_path, style, prompt_arg, cfg)
        if dry_run:
            # D-2918: a refusing dry run is still dry; a passing one writes
            # nothing either - planned argv, cwd and env keys only.
            return {"argv": self._shown(bg_argv, prompt_arg), "cwd": str(cwd_path),
                    "env_keys": sorted(k for k in ("FLEET_SESSION_NAME", "CLAUDE_CODE_AUTO_COMPACT_WINDOW"))}
        sid, warning = self._run_launch(name, bg_argv, wt_argv, env, launcher, cwd_path, prompt_arg)
        prompt_path = self._sessions / f"{name}.prompt.txt"
        atomic_write_private(prompt_path, text)
        rec = {"name": name, "cwd": str(cwd_path), "model": model, "effort": effort,
               "mcp": str(mcp_path), "prompt_file": str(prompt_path),
               "handoff": str(handoff_path) if handoff_path else None,
               "parent": parent, "children": [], "ts": self._now(),
               "session_id": sid, "job_id": sid[:8] if sid else None,
               "permission_mode": mode, "platform": self._platform,
               "launcher": launcher, "stopped_ts": None}
        self._write_record(rec)
        result = {"ok": True, "name": name, "session_id": sid,
                  "_audit_argv": self._shown(bg_argv if launcher == "bg" else wt_argv, prompt_arg)}
        if warning:
            result["warning"] = warning
        return result

    def stop(self, name=None, session_id=None):
        return self._action("stop", name or session_id or "", lambda: self._stop_inner(name, session_id))

    def _stop_inner(self, name, session_id):
        if not name and not session_id:
            raise _refuse("stop needs a name or id")
        if name:
            self._check_name(name)
        rec = self._find_record(name, session_id)
        if rec is None:
            raise _refuse(f"unknown session: {name or session_id}")
        sid = rec.get("session_id")
        if not sid:
            raise _refuse(f"no session id recorded for {rec.get('name')}")
        self._stop_record(rec)
        return {"ok": True, "name": rec.get("name"), "session_id": sid}

    def _stop_record(self, rec):
        """claude stop <id> - the one and only stop mechanism, never rm/kill."""
        proc = self._run(["claude", "stop", rec.get("session_id")],
                         capture_output=True, text=True, timeout=STOP_TIMEOUT)
        if proc.returncode != 0:
            raise _refuse(f"stop failed: claude stop exited {proc.returncode}")
        rec["stopped_ts"] = self._now()
        self._write_record(rec)

    def mark_ready(self, name, handoff, card=None):
        return self._action("mark_ready", name, lambda: self._mark_ready_inner(name, handoff, card))

    def _mark_ready_inner(self, name, handoff, card):
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

    def relaunch(self, name, handoff=None, force=False):
        return self._action("relaunch", name, lambda: self._relaunch_inner(name, handoff, force))

    def _relaunch_handoff(self, rec, cfg, name, handoff, force):
        """Effective relaunch handoff: the argument wins, else the ready marker.
        Freshness is waived under force, never existence or containment. A bare
        forced relaunch carries no handoff at all - never the record's old one."""
        if handoff is not None:
            path = self._handoff_arg(handoff, cfg)
        else:
            marker_path = self._marker_path(name)
            if not marker_path.is_file():
                if force:
                    return None
                raise _refuse("ready marker required to relaunch; pass force=True to relaunch bare")
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                marker = {}
            raw = marker.get("handoff")
            if not isinstance(raw, str) or not raw:
                raise _refuse("ready marker handoff must be a path string")
            path = self._handoff_arg(raw, cfg)
        if not force and path.stat().st_mtime < float(rec.get("ts") or 0):
            raise _refuse(f"handoff is not newer than the session record: {path}")
        return path

    def _relaunch_inner(self, name, handoff, force):
        if not isinstance(force, bool):
            raise _refuse("force must be a real boolean")
        cfg = self._load_config()
        name = self._check_name(name)
        rec = self._find_record(name, None)
        if rec is None:
            raise _refuse(f"unknown session: {name}")
        handoff_path = self._relaunch_handoff(rec, cfg, name, handoff, force)
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
        bg_argv, wt_argv, env, launcher = self._build_argv(
            name, cwd_path, model, effort, mode, mcp_path, style, prompt_arg, cfg)
        old_sid = rec.get("session_id")
        if old_sid and rec.get("stopped_ts") is None:
            self._stop_record(rec)  # raises "stop failed" -> nothing is launched
        try:
            sid, warning = self._run_launch(
                name, bg_argv, wt_argv, env, launcher, cwd_path, prompt_arg, exclude=old_sid)
        except _ActionFailed:
            # The old session is already stopped: keep its record (with
            # stopped_ts) and the ready marker so a plain retry can relaunch.
            raise
        rec.update({"session_id": sid, "job_id": sid[:8] if sid else None,
                    "stopped_ts": None, "ts": self._now(),
                    "handoff": str(handoff_path) if handoff_path else None,
                    "launcher": launcher})
        self._write_record(rec)
        self._marker_path(name).unlink(missing_ok=True)
        result = {"ok": True, "name": name, "session_id": sid,
                  "_audit_argv": self._shown(bg_argv if launcher == "bg" else wt_argv, prompt_arg)}
        if warning:
            result["warning"] = warning
        return result

    def list(self):
        return self._action("list", "", self._list_inner)

    def _list_inner(self):
        cfg = self._load_config()
        cap = cfg.get("ctx_cap_tokens") or 200000
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
                "state": agent.get("state") or ("stopped" if rec.get("stopped_ts") else "running"),
                "ready": self._marker_path(rec.get("name")).is_file(),
            })
        return {"sessions": sessions}

    def _context_tokens(self, cwd, sid):
        """Latest assistant usage in the transcript - the live context size."""
        if not cwd or not sid:
            return 0
        try:
            lines = transcript_path(cwd, sid).read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        total = 0
        for line in lines:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            usage = (obj.get("message") or {}).get("usage") if isinstance(obj, dict) else None
            if isinstance(usage, dict):
                total = sum(int(usage.get(key) or 0) for key in USAGE_KEYS)
        return total
