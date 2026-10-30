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
import stat
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
TAIL_BYTES = 256 * 1024  # spec: only the transcript's last 256 KB is read
# `claude agents --json` states that mean the old session is already over;
# any other (or missing) state is live and must be stopped before a relaunch
# replaces it - guessing wrong can only stop what should be stopped.
FINISHED_STATES = frozenset({"finished", "stopped"})


_SCRUB_KEYS = frozenset({"reason", "error", "warning", "card", "message", "argv"})


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
    (injectables), ``now`` (clock), ``platform``, ``resolve_budget`` (seconds to
    keep polling ``claude agents`` for the new session's row, 0.5 s apart),
    ``which`` (exe resolver, consulted on win32 only), ``sleep`` (poll wait).
    """

    def __init__(self, state_root=None, *, config_path=None, run=None, popen=None,
                 now=None, platform=None, resolve_budget=15.0, which=None, sleep=None):
        self._run = run if run is not None else subprocess.run
        self._popen = popen if popen is not None else subprocess.Popen
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

    # ---- config: parsed, trusted, fail-closed (R-1, R-3) ---------------

    def _config_file(self):
        """The configured path, else $PLANGRAPH_SESSIONS_CONFIG, else the
        default ~/.config/plangraph/sessions.json (R-24)."""
        return Path(self._config_path) if self._config_path else (
            Path.home() / ".config" / "plangraph" / "sessions.json")

    def _trust(self, path, what, *, directory=False, nofollow=False):
        """POSIX trust checks (skipped on win32): open the path and fstat the
        descriptor - never stat by name - so a swapped symlink cannot lie about
        what was checked. The result must be a regular file (or directory),
        owned by the current user and not group/world-writable (R-24).
        Symlinks are followed on purpose except for the config itself: a
        symlink alias to an allowed file is not a finding."""
        if self._platform == "win32":
            return
        flags = os.O_RDONLY
        if nofollow and hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(path), flags)
        except OSError:
            raise _refuse(f"{what} is not a readable regular file: {path}")
        try:
            self._trust_stat(os.fstat(fd), path, what, directory=directory)
        finally:
            os.close(fd)

    def _trust_stat(self, st, path, what, *, directory=False):
        """The fstat side of the trust checks: kind, owner, mode (R-24)."""
        kind_ok = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
        if not kind_ok:
            raise _refuse(f"{what} is not a regular file: {path}")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise _refuse(f"{what} is not owned by the current user: {path}")
        if st.st_mode & 0o022:
            raise _refuse(f"{what} is group/world-writable: {path}")

    def _read_trusted(self, path, what, *, nofollow=False):
        """Open once, run the trust checks on the descriptor and read the
        content from that same fd (R-24): the bytes obeyed are the bytes that
        passed the checks - never re-open the path by name afterwards."""
        flags = os.O_RDONLY
        if nofollow and hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(path), flags)
        except OSError:
            raise _refuse(f"{what} is not a readable regular file: {path}")
        try:
            if self._platform != "win32":
                self._trust_stat(os.fstat(fd), path, what)
            chunks = []
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    raise _refuse(f"{what} is not a readable regular file: {path}")
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _load_config(self):
        path = self._config_file()
        try:
            path.stat()
        except OSError:
            raise _refuse(f"sessions config not found: {path}")
        if self._platform != "win32":
            # POSIX trust: the config is only ours to obey if it is ours alone -
            # it may not even be a symlink, and neither its directory nor the
            # files it points at may be writable by anyone else (R-24).
            if os.path.islink(path):
                raise _refuse(f"sessions config must not be a symlink: {path}")
            self._trust(path.parent, "sessions config directory", directory=True)
        # The JSON is read from the descriptor the trust checks ran on, never
        # re-opened by name after they passed (R-24). The read sits outside
        # the parse guard: ParameterRefused is a ValueError and must not be
        # re-read as "not valid JSON".
        raw = self._read_trusted(path, "sessions config", nofollow=True)
        try:
            cfg = json.loads(raw)
        except ValueError:
            raise _refuse(f"sessions config is not valid JSON: {path}")
        if not isinstance(cfg, dict):
            raise _refuse(f"sessions config must be a JSON object: {path}")
        self._check_config(cfg)
        return cfg

    def _check_config(self, cfg):
        unknown = sorted(set(cfg) - SESSION_CONFIG_KEYS)
        if unknown:
            raise _refuse(f"config has unknown keys: {unknown}")
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
            entry = Path(raw).expanduser().resolve()
            # The entry's directory counts too: a writable dir lets someone
            # swap the file after the check (the sub-dir finding).
            self._trust(entry.parent, "config mcp_configs entry directory", directory=True)
            self._trust(entry, "config mcp_configs entry")
        # Scalars: a present key must carry its type - a null is not a value.
        self._style_arg(cfg)
        if "compact_window" in cfg:
            window = cfg["compact_window"]
            if not isinstance(window, str) or not window.isdigit():
                raise _refuse("config compact_window must be a digit string")
        if "ctx_cap_tokens" in cfg:
            cap = cfg["ctx_cap_tokens"]
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                raise _refuse("config ctx_cap_tokens must be a positive int")
        if "windows_launcher" in cfg and cfg["windows_launcher"] not in ("bg", "wt"):
            raise _refuse("config windows_launcher must be 'bg' or 'wt'")

    def _style_arg(self, cfg):
        """style_file: resolves inside cwd_roots/file_roots, is a regular file
        that is not a symlink (lstat) and passes the owner/mode trust checks
        (R-24). Ancestor directories beyond the immediate parent are covered
        by the documented same-user limit, not checked here."""
        if "style_file" not in cfg:
            return None
        style = cfg["style_file"]
        if not isinstance(style, str):
            raise _refuse(f"config style_file must be a string: {style!r}")
        if not Path(style).is_absolute():
            raise _refuse(f"config style_file must be an absolute readable file: {style}")
        raw = Path(style)
        try:
            mode = os.lstat(raw).st_mode
        except OSError:
            raise _refuse(f"config style_file must be an absolute readable file: {style}")
        if stat.S_ISLNK(mode):
            raise _refuse(f"config style_file must not be a symlink: {style}")
        path = raw.resolve()
        if not self._inside(path, self._file_roots(cfg)):
            raise _refuse(f"config style_file is outside the configured roots: {style}")
        if not path.is_file():
            raise _refuse(f"config style_file must be an absolute readable file: {style}")
        self._trust(path.parent, "config style_file directory", directory=True)
        self._trust(path, "config style_file")
        return path

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
        # The resolved path is the listed one: a symlink alias to an allowed
        # file is not a finding, but the target must still be trusted (R-24).
        self._trust(path, "mcp_config")
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
        path = self._style_arg(cfg)
        if path is None:
            return None
        # The text is read from the descriptor the checks ran on (R-24).
        return self._read_trusted(path, "config style_file", nofollow=True).decode("utf-8").strip()

    def _handoff_arg(self, handoff, cfg):
        if not isinstance(handoff, str) or not handoff:
            raise _refuse("handoff must be a non-empty path string")
        return self._file_arg(handoff, cfg, "handoff")

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
        # wt.exe splits its command line on unescaped ';' (R-17): every ';'
        # in every argument after "new-tab" is escaped as wt documents.
        wt_tail = [arg.replace(";", "\\;") for arg in
                   ["--title", name, "-d", str(cwd_path)] + head + tail]
        wt_argv = ["wt.exe", "-w", "0", "new-tab"] + wt_tail
        # Spec defaults: ctx_cap_tokens 300000, compact_window "330000".
        cap = cfg.get("ctx_cap_tokens") or 300000
        compact = cfg.get("compact_window") or "330000"
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
        # Head resolution refusals propagate out (ParameterRefused is not one
        # of the swallowed errors): polling must never fall back to a bare
        # "claude" on win32 (R-9).
        head = self._claude_head()
        try:
            proc = self._run(head + ["agents", "--json"], capture_output=True, text=True, timeout=STOP_TIMEOUT)
            rows = json.loads(proc.stdout or "[]")
            return rows if isinstance(rows, list) else []
        except (OSError, subprocess.SubprocessError, ValueError):
            return []

    def _resolve_id(self, name, stdout, exclude=None, launched_ms=None):
        """Map a just-launched session to its real session id. A full UUID in
        the launcher's own stdout wins outright (unless it is the excluded,
        replaced id); otherwise `claude agents` is polled every 0.5 s until the
        resolve budget expires: the short id matches a row `id` or a `sessionId`
        prefix, else the newest `background` row by name + startedAt near the
        launch (launched_ms is taken before the process starts, R-18). The
        previous session id is excluded from every lookup - full UUID, short id
        and fallback alike - so a relaunch can never resolve to the session it
        replaced."""
        if launched_ms is None:
            launched_ms = self._now() * 1000.0
        found = UUID_RE.search(stdout or "")
        if found and found.group(0) != exclude:
            return found.group(0)
        cutoff = launched_ms - 5000.0
        attempts = max(1, int(round(self._resolve_budget / 0.5)))
        for attempt in range(attempts):
            rows = self._agents_rows()
            short = SHORT_RE.search(stdout or "")
            if short:
                token = short.group(1)
                for row in rows:
                    sid = row.get("sessionId")
                    if not sid or sid == exclude:
                        continue
                    if str(row.get("id") or "") == token or str(sid).startswith(token):
                        return sid
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
            if best is not None:
                return best.get("sessionId")
            if attempt + 1 < attempts:
                self._sleep(0.5)
        return None

    def _run_launch(self, name, bg_argv, wt_argv, env, launcher, cwd_path, prompt_arg, exclude=None):
        """Start the session; returns (session_id, warning). Raises _ActionFailed
        on a non-zero exit and TimeoutExpired / OSError upward to the wrapper.
        `exclude` is the previous session id on a relaunch: it must never be
        resolved as the new session's id (R-13)."""
        launched_ms = self._now() * 1000.0  # before the start: rows near it count
        if launcher == "wt":
            kw = {k: v for k, v in self._launch_run_kw(env).items() if k not in ("capture_output", "text")}
            self._popen(wt_argv, cwd=str(cwd_path), **kw)
            # wt only exposes name + startedAt; the launcher's stdout is ignored.
            sid = self._resolve_id(name, "", exclude, launched_ms)
        else:
            proc = self._run(bg_argv, cwd=str(cwd_path), **self._launch_run_kw(env))
            if proc.returncode != 0:
                raise _ActionFailed(f"claude exited {proc.returncode}")
            sid = self._resolve_id(name, proc.stdout or "", exclude, launched_ms)
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
        bg_argv, wt_argv, env, launcher = self._build_argv(
            name, cwd_path, model, effort, mode, mcp_path, style, prompt_arg, cfg)
        self._audit_argv = self._shown(bg_argv if launcher == "bg" else wt_argv, prompt_arg)
        existing = self._find_record(name, None)
        if existing is not None and existing.get("stopped_ts") is None:
            # A live record is only replaced through relaunch (R-19).
            raise _refuse(f"session {name} already has an active record; use relaunch")
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
               "launcher": launcher, "stopped_ts": None}
        try:
            sid, warning = self._run_launch(name, bg_argv, wt_argv, env, launcher, cwd_path, prompt_arg)
        except (subprocess.TimeoutExpired, OSError):
            # The launch failed AFTER the process may have started: the record
            # is still written - session_id null, stopped_ts None - so list
            # shows the possible orphan and relaunch force can replace it.
            # The write is best effort: the launch failure stays the outcome.
            rec["ts"] = self._now()
            try:
                self._write_record(name, rec)
            except (OSError, ValueError):
                pass
            raise
        rec.update({"session_id": sid, "job_id": sid[:8] if sid else None, "ts": self._now()})
        self._write_record(name, rec)
        self._marker_path(name).unlink(missing_ok=True)  # a stale marker never outlives a launch
        result = {"ok": True, "name": name, "session_id": sid,
                  "_audit_argv": self._shown(bg_argv if launcher == "bg" else wt_argv, prompt_arg)}
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
        sid = rec.get("session_id")
        if not sid:
            raise _refuse(f"no session id recorded for {rec.get('name')}")
        self._stop_record(rec, name)
        return {"ok": True, "name": name, "session_id": sid}

    def _stop_record(self, rec, name):
        """claude stop <id> - the one and only stop mechanism, never rm/kill.
        The argv head is the resolved _claude_head() like every other call."""
        proc = self._run(self._claude_head() + ["stop", rec.get("session_id")],
                         capture_output=True, text=True, timeout=STOP_TIMEOUT)
        if proc.returncode != 0:
            raise _refuse(f"stop failed: claude stop exited {proc.returncode}")
        rec["stopped_ts"] = self._now()
        self._write_record(name, rec)

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
        old_sid = rec.get("session_id")
        no_old_id = old_sid is None
        if no_old_id and not force:
            # With no recorded id the old session cannot be stopped: force
            # acknowledges that and carries a warning (R-20).
            raise _refuse(f"record has no session id for {name}; pass force=True to relaunch")
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
        self._audit_argv = self._shown(bg_argv if launcher == "bg" else wt_argv, prompt_arg)
        if old_sid and rec.get("stopped_ts") is None:
            self._stop_record(rec, name)  # raises "stop failed" -> nothing is launched
        elif old_sid:
            # The record says stopped, but the session may have outlived its
            # stop: `claude agents --json` is the source of truth. A listed id
            # outside a finished/stopped state is stopped first - a failed stop
            # refuses the relaunch, nothing is launched - while an unlisted id
            # is gone for good and proceeds untouched.
            row = next((r for r in self._agents_rows()
                        if r.get("sessionId") == old_sid), None)
            if row is not None and str(row.get("state") or "") not in FINISHED_STATES:
                self._stop_record(rec, name)
        new_rec = dict(rec)
        try:
            sid, warning = self._run_launch(
                name, bg_argv, wt_argv, env, launcher, cwd_path, prompt_arg, exclude=old_sid)
            new_rec.update({"session_id": sid, "job_id": sid[:8] if sid else None,
                            "stopped_ts": None, "ts": self._now(),
                            "handoff": str(handoff_path) if handoff_path else None,
                            "launcher": launcher})
            self._write_record(name, new_rec)
        except BaseException:
            # ANY failure after the stop keeps the old record (with stopped_ts)
            # and the ready marker so a plain retry can relaunch; the marker is
            # removed only after the new record is written (R-21).
            if rec.get("stopped_ts") is None:
                rec["stopped_ts"] = self._now()
            try:
                self._write_record(name, rec)
            except (OSError, ValueError):
                pass  # the disk already holds the old record
            raise
        self._marker_path(name).unlink(missing_ok=True)
        result = {"ok": True, "name": name, "session_id": sid,
                  "_audit_argv": self._shown(bg_argv if launcher == "bg" else wt_argv, prompt_arg)}
        warnings = []
        if no_old_id:
            warnings.append("previous session id was unknown; the old session was not stopped")
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
                "state": agent.get("state") or ("stopped" if rec.get("stopped_ts") else "running"),
                "ready": self._marker_path(rec.get("name")).is_file(),
            })
        return {"sessions": sessions}

    def _context_tokens(self, cwd, sid):
        """Latest assistant usage in the transcript - the live context size.
        Only the last 256 KB of the file is read (the spec's tail window):
        the latest usage sits near the end and transcripts grow unbounded,
        so a full read would cost more forever for no extra signal."""
        if not cwd or not sid:
            return 0
        try:
            with transcript_path(cwd, sid).open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - TAIL_BYTES))
                blob = fh.read()
        except OSError:
            return 0
        total = 0
        for line in blob.decode("utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            usage = (obj.get("message") or {}).get("usage") if isinstance(obj, dict) else None
            if isinstance(usage, dict):
                total = sum(int(usage.get(key) or 0) for key in USAGE_KEYS)
        return total
