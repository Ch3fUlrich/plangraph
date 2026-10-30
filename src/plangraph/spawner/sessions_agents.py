"""Agent poll, argv construction and launch/resolve for Sessions.

Body moved unchanged out of sessions.py (SESSION-SPAWN-MCP slice 2/4) so both
files stay inside the 400-line file gate (D-3209); Sessions picks these up
through _AgentsMixin and sessions.py re-exports the module names."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

LAUNCH_TIMEOUT = 60
STOP_TIMEOUT = 30
DETACHED = 0x8 | 0x200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SHORT_RE = re.compile(r"backgrounded\s*\W*([0-9a-fA-F]{8})\b")
USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
TAIL_BYTES = 256 * 1024  # spec: only the transcript's last 256 KB is read
# `claude agents --json` states that mean the old session is already over;
# any other (or missing) state is live and must be stopped before a relaunch
# replaces it - guessing wrong can only stop what should be stopped.
FINISHED_STATES = frozenset({"finished", "stopped"})


def transcript_path(cwd, session_id):
    """Claude transcript file for one session: every non-alphanumeric cwd
    character maps to "-" (spelled out by the tests on purpose), the session id
    is the file stem."""
    safe = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / safe / f"{session_id}.jsonl"


class _ActionFailed(Exception):
    """Subprocess-level failure: audited, then reported as {"ok": False}."""

    def __init__(self, msg="", session_id=None):
        super().__init__(msg)
        self.session_id = session_id


class _AgentsMixin:
    """Launch/resolve internals of Sessions: argv is always a list, never a
    shell string (D-2873). Instances carry the injectable runner, clock and
    platform that Sessions.__init__ sets (self._run, self._now, self._sleep,
    self._which, self._platform, self._resolve_budget)."""

    # ---- subprocess argv construction (D-2873) -------------------------

    def _claude_head(self):
        if self._platform != "win32":
            return ["claude"]
        # A .cmd/.bat shim would let cmd.exe parse the prompt: only the real
        # .exe is ever handed to CreateProcess or Windows Terminal (R-9).
        resolved = self._which("claude")
        if not resolved:
            raise _refuse("claude not found on PATH; refusing shim-less Windows launch")
        if not str(resolved).lower().endswith(".exe"):
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
        # Spec defaults: ctx_cap_tokens 300000, compact_window "330000".
        cap = cfg.get("ctx_cap_tokens") or 300000
        compact = cfg.get("compact_window") or "330000"
        env = {**os.environ, "FLEET_SESSION_NAME": name,
               "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(compact)}
        return bg_argv, env

    def _launch_run_kw(self, env):
        kw = {"capture_output": True, "text": True, "env": env, "timeout": LAUNCH_TIMEOUT}
        if self._platform == "win32":
            kw["creationflags"] = DETACHED
        return kw

    @staticmethod
    def _shown(argv, prompt_arg):
        shown = list(argv)
        shown[-1] = f"<{len(prompt_arg)} chars>"
        # The value after --append-system-prompt is config style prose like the
        # prompt: log its length, never its text (SESSION-SPAWN-MCP).
        for i, arg in enumerate(argv):
            if arg == "--append-system-prompt" and i + 1 < len(argv):
                shown[i + 1] = f"<{len(argv[i + 1])} chars>"
        return shown

    def _agents_rows(self):
        # Lenient by design: _resolve_id polls repeatedly and list degrades to
        # "no agent rows", so a poll failure must not abort the action.
        try:
            return self._agents_rows_strict()
        except _ActionFailed:
            return []

    def _agents_rows_strict(self):
        # Fail-closed poll for decisions that gate a launch or relaunch: when
        # the poll cannot say whether an old id is still live, the action is
        # refused, never passed (round 4). Head resolution refusals propagate
        # out (ParameterRefused is not one of the swallowed errors): polling
        # must never fall back to a bare "claude" on win32 (R-9).
        head = self._claude_head()
        try:
            proc = self._run(head + ["agents", "--json"], capture_output=True, text=True, timeout=STOP_TIMEOUT)
            rows = json.loads(proc.stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise _ActionFailed(f"agents poll failed: {exc}") from exc
        if not isinstance(rows, list):
            raise _ActionFailed(f"agents poll failed: JSON is not a list: {type(rows).__name__}")
        if any(not isinstance(row, dict) for row in rows):
            # A malformed row must fail closed, never read as absence: acting
            # on a poll that cannot describe the agents list risks a duplicate
            # launch or an unconfirmed stop (r5 seat).
            raise _ActionFailed("agents poll failed: row is not an object")
        return rows

    def _resolve_id(self, name, stdout, exclude=None, launched_ms=None):
        """Map a just-launched session to its real session id. A full UUID in
        the launcher's own stdout wins outright (unless it is the excluded,
        replaced id); otherwise `claude agents` is polled every 0.5 s until the
        resolve budget expires: the short id matches a `background` row of the
        launched name by `id` or `sessionId` prefix (a short id alone is not
        authority: name and kind must already identify the launched session's
        own row), else the newest `background` row by name + startedAt near the
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
                    if row.get("name") != name or row.get("kind") != "background":
                        continue
                    if str(row.get("state") or "") in FINISHED_STATES:
                        continue  # a finished/stopped row is never the new id
                    sid = row.get("sessionId")
                    if not sid or sid == exclude:
                        continue
                    if str(row.get("id") or "") == token or str(sid).startswith(token):
                        return sid
            best = None
            for row in rows:
                if row.get("name") != name or row.get("kind") != "background":
                    continue
                if str(row.get("state") or "") in FINISHED_STATES:
                    continue  # a finished/stopped row is never the new id
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

    def _run_launch(self, name, bg_argv, env, cwd_path, prompt_arg, exclude=None,
                    launched_ms=None):
        """Start the session; returns (session_id, warning). Raises _ActionFailed
        on a non-zero exit and TimeoutExpired / OSError upward to the wrapper.
        `exclude` is the previous session id on a relaunch: it must never be
        resolved as the new session's id (R-13). `launched_ms` is the clock
        reading taken before the start (R-18) when the caller already wrote it
        into the write-ahead record; it defaults to a fresh pre-start reading."""
        if launched_ms is None:
            launched_ms = self._now() * 1000.0  # before the start: rows near it count
        proc = self._run(bg_argv, cwd=str(cwd_path), **self._launch_run_kw(env))
        if proc.returncode != 0:
            # First 300 chars of the process output: enough to see why claude
            # refused (e.g. untrusted workspace) without unbounded audit text.
            detail = ((proc.stdout or "") + (proc.stderr or "")).strip()[:300]
            raise _ActionFailed(f"claude exited {proc.returncode}: {detail}")
        sid = self._resolve_id(name, proc.stdout or "", exclude, launched_ms)
        return sid, (None if sid else "session id could not be resolved")

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
            if not isinstance(obj, dict):
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            # Only assistant entries describe the live context: a later
            # user/tool line carrying a usage dict must not win (qwen LOW).
            if obj.get("type") != "assistant" and msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if isinstance(usage, dict):
                total = sum(int(usage.get(key) or 0) for key in USAGE_KEYS)
        return total


# Imported last on purpose, mirroring sessions_config's own tail: that module
# binds _scrub from sessions at its end, so importing it can pull sessions.py
# in mid-flight, and sessions.py in turn imports this module. Every name
# sessions.py re-exports (_AgentsMixin, transcript_path, _ActionFailed and the
# constants) is defined above before this import runs, so either import order
# resolves; moving it to the top would break one of them.
from plangraph.spawner.sessions_config import _refuse
