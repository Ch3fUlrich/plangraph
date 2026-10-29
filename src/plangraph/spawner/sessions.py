"""Claude Code session lifecycle (SESSION-SPAWN-MCP, docs/spawner-sessions.md).
Ports routing bin/fleet-launch (build_cmd, resolve) and bin/routerctl (transcript, ctx).
Injectable run/popen callables; argv is always a list; refusals raise ParameterRefused.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from plangraph.routing_errors import ParameterRefused
from .env_redact import redact_env_values
from .private_io import append_private, atomic_write_private

NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SHORT_ID = re.compile(r"backgrounded\W+([0-9a-f]{8})\b")
STARTED_SLACK_MS = 5000
RESOLVE_CALL_TIMEOUT, RESOLVE_WAIT = 5.0, 0.5
CTX_TAIL = 256 * 1024
DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP = 0x8, 0x200
REQUIRED_KEYS = ("cwd_roots", "models", "efforts", "permission_modes", "mcp_configs")

def _started_ms(row):
    v = row.get("startedAt")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

def transcript_path(cwd, session_id):
    """routerctl.transcript_path: the Claude Code transcript of one session."""
    return (Path.home() / ".claude/projects" / re.sub(r"[/.]", "-", str(cwd)) / f"{session_id}.jsonl") if cwd and session_id else None

def context_tokens(cwd, session_id):
    """routerctl.context_tokens: last assistant usage (input + caches), tail read only."""
    f = transcript_path(cwd, session_id)
    if f is None:
        return None
    try:
        with f.open("rb") as fh:
            start = max(0, fh.seek(0, 2) - CTX_TAIL)
            fh.seek(start)
            lines = fh.read().decode("utf-8", "replace").splitlines()[1 if start else 0:]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            j = json.loads(line) if '"usage"' in line else {}
            if j.get("type") == "assistant":
                u = j["message"]["usage"]
                vals = [u.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
                if all(type(v) is int for v in vals):
                    return sum(vals)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return None

def build_cmd(name, cwd, model, effort, mcp, permission_mode, prompt, style, platform, launcher):
    """argv list for one launch; a list only, never a shell string (D-3142)."""
    flags = ["--strict-mcp-config", "--mcp-config", str(mcp), "--remote-control", name, "-n", name, "--model", model, "--effort", effort, "--permission-mode", permission_mode]
    if style:
        flags += ["--append-system-prompt", style]
    if platform == "win32" and launcher == "wt":
        return ["wt.exe", "-w", "0", "new-tab", "--title", name, "-d", str(cwd), "claude"] + flags + [prompt]
    return ["claude", "--bg"] + flags + [prompt]

def _scrub(obj):
    """redact_env_values over every string before it reaches record or audit."""
    if isinstance(obj, str):
        return redact_env_values(obj, dict(os.environ))
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return {k: _scrub(v) for k, v in obj.items()} if isinstance(obj, dict) else obj

def _load_config(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise ParameterRefused(f"config {path} missing or invalid; refusing to launch") from None
    ok = (isinstance(data, dict) and all(isinstance(data.get(k), list) and data[k]
              and all(isinstance(v, str) and v for v in data[k]) for k in REQUIRED_KEYS)
          and all(Path(v).is_absolute() for k in ("cwd_roots", "mcp_configs") for v in data[k])
          and data.get("windows_launcher", "bg") in ("bg", "wt") and ("style_file" not in data or isinstance(data["style_file"], str))
          and type(data.get("ctx_cap_tokens", 1)) is int and data.get("ctx_cap_tokens", 1) > 0)
    if not ok:
        raise ParameterRefused(f"config {path} invalid (fail closed)")
    return data

def _check_name(name, what="name"):
    if not NAME_RE.fullmatch(str(name or "")): raise ParameterRefused(f"invalid {what} {name!r}: want [A-Za-z0-9._-] 1-64")

def _inside_roots(cwd, roots):
    try:
        resolved = Path(cwd).resolve(strict=True)  # realpath: a symlink cannot escape a root
    except OSError:
        raise ParameterRefused(f"cwd {cwd!r} not resolvable") from None
    if not resolved.is_dir(): raise ParameterRefused(f"cwd {cwd!r} is not a directory")
    for root in roots:
        if resolved == (r := Path(root).resolve()) or r in resolved.parents:
            return resolved
    raise ParameterRefused(f"cwd {cwd!r} is outside config cwd_roots")

def _readable_file(path, what):
    try:
        p = Path(path).resolve(strict=True)
        with p.open("rb"):
            pass
        if not p.is_file():
            raise OSError
    except OSError:
        raise ParameterRefused(f"{what} {path!r} not a readable regular file") from None
    return p

def _read_prompt(prompt, prompt_file):
    """Exactly one of prompt / prompt_file; returns (text, source_path_or_None)."""
    if (prompt is None) == (prompt_file is None):
        raise ParameterRefused("exactly one of prompt or prompt_file required")
    if prompt is not None:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ParameterRefused("prompt must be a non-empty string")
        return prompt, None
    p = _readable_file(prompt_file, "prompt_file")
    text = p.read_text()  # the probe above proved readability
    if not text.strip():
        raise ParameterRefused("prompt must be non-empty")
    return text, str(p)

def _match_mcp(mcp_config, allowed):
    cand = Path(mcp_config).resolve()
    for entry in allowed:
        if cand == Path(entry).resolve():
            return cand
    raise ParameterRefused(f"mcp_config {mcp_config!r} not in config allowlist")

def _read_style(style_file):
    return _readable_file(style_file, "style_file").read_text().rstrip("\n") if style_file else ""

class Sessions:
    """Claude Code session lifecycle over one state root (SpawnService.state)."""

    def __init__(self, state_root, config_path=None, run=None, popen=None, now=None, platform=None, resolve_budget=15.0):
        self.sessions_dir = Path(state_root) / "sessions"
        self.config_path = Path(config_path or os.environ.get("PLANGRAPH_SESSIONS_CONFIG")
                                or Path.home() / ".config/plangraph/sessions.json")
        self.run, self.popen = run or subprocess.run, popen or subprocess.Popen
        self.now, self.platform = now or time.time, (sys.platform if platform is None else platform)
        self.resolve_budget = resolve_budget

    def _path(self, name, suffix):
        return self.sessions_dir / f"{name}{suffix}"

    def _read_json(self, path):
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _records(self):
        out = []
        for p in sorted(self.sessions_dir.glob("*.json")):
            data = self._read_json(p)
            if not p.name.endswith(".ready.json") and data and isinstance(data.get("name"), str):
                out.append(data)
        return out

    def _audit(self, action, name, session_id, ok, reason=None, argv=None):
        """One JSON line per action; argv keeps only '<N chars>' where the prompt was."""
        line = {"ts": self.now(), "action": action, "name": name, "session_id": session_id, "ok": bool(ok),
                "caller": os.environ.get("PLANGRAPH_PRINCIPAL") or os.environ.get("USER") or "unknown",
                "reason": reason, **({"argv": list(argv)} if argv is not None else {})}
        append_private(self.sessions_dir / "audit.log", json.dumps(_scrub(line)) + "\n")

    def _agents_rows(self):
        try:
            p = self.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=RESOLVE_CALL_TIMEOUT)
            rows = json.loads(p.stdout) if p.returncode == 0 else []
        except (OSError, ValueError, subprocess.SubprocessError):
            rows = []
        return [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict) and isinstance(r.get("sessionId"), str)]

    def _resolve(self, stdout, name, launched_ms, launcher):
        short = None
        if launcher != "wt":  # wt has no stdout: name + startedAt lookup only
            m, s = UUID.search(stdout or ""), SHORT_ID.search(stdout or "")
            if m:
                return m.group(0)
            short = s.group(1) if s else None
        deadline = time.monotonic() + self.resolve_budget
        while True:
            rows = self._agents_rows()
            if short:
                hit = [r for r in rows if r.get("id") == short or r["sessionId"].startswith(short)]
            else:  # newest fresh background row; a stale same-name session never matches
                hit = sorted([r for r in rows if r.get("name") == name and r.get("kind") == "background"
                              and (_started_ms(r) or -1) >= launched_ms - STARTED_SLACK_MS], key=_started_ms, reverse=True)[:1]
            if hit and UUID.fullmatch(hit[0]["sessionId"]): return hit[0]["sessionId"]
            if time.monotonic() + RESOLVE_WAIT >= deadline:
                return None
            time.sleep(RESOLVE_WAIT)

    def launch(self, name, cwd, model, effort, mcp_config, permission_mode, prompt=None, prompt_file=None,
               handoff=None, parent=None, dry_run=False, children=None, audit=True):
        cfg = _load_config(self.config_path)  # fail closed before anything runs
        _check_name(name)
        if parent is not None:
            _check_name(parent, "parent")
        root = _inside_roots(cwd, cfg["cwd_roots"])
        text, source = _read_prompt(prompt, prompt_file)
        mcp = _match_mcp(mcp_config, cfg["mcp_configs"])
        for key, val, allowed in (("model", model, cfg["models"]), ("effort", effort, cfg["efforts"]),
                                  ("permission_mode", permission_mode, cfg["permission_modes"])):
            if val not in allowed: raise ParameterRefused(f"{key} {val!r} not in config allowlist")
        hand = _readable_file(handoff, "handoff") if handoff else None
        launcher = cfg.get("windows_launcher", "bg")
        prompt_argv = (f"Restart from handoff {hand}.\n\n" + text) if hand else text
        env = dict(os.environ, CLAUDE_CODE_AUTO_COMPACT_WINDOW=str(cfg.get("compact_window", "330000")), FLEET_SESSION_NAME=name)
        argv = build_cmd(name, str(root), model, effort, mcp, permission_mode, prompt_argv,
                         _read_style(cfg.get("style_file")), self.platform, launcher)
        shown = argv[:-1] + [f"<{len(argv[-1])} chars>"]  # prompt never reaches logs
        if dry_run:  # D-2918: nothing written, nothing started
            return {"argv": shown, "cwd": str(root), "env_keys": sorted(env)}
        if source is None:  # inline prompts get a 0600 file
            atomic_write_private(self._path(name, ".prompt.txt"), text)
        prompt_out = source or str(self._path(name, ".prompt.txt"))
        flags = {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP} if self.platform == "win32" else {}
        launched_ms, stdout = int(self.now() * 1000), ""
        if self.platform == "win32" and launcher == "wt":
            self.popen(argv, cwd=str(root), env=env, **flags)  # detached, no wait
        else:
            p = self.run(argv, cwd=str(root), env=env, capture_output=True, text=True, **flags)
            stdout = p.stdout or ""
            if p.returncode != 0:
                if audit:
                    self._audit("launch", name, None, False, f"exit {p.returncode}", shown)
                return {"ok": False, "name": name, "error": f"claude exited {p.returncode}", "session_id": None}
        session_id = self._resolve(stdout, name, launched_ms, launcher)
        warning = None if session_id else f"no session id for {name}; stop will refuse"
        record = {"name": name, "cwd": str(root), "model": model, "effort": effort, "mcp": str(mcp),
                  "prompt_file": prompt_out, "handoff": str(hand) if hand else None, "parent": parent,
                  "children": list(children or []), "ts": int(self.now()), "session_id": session_id,
                  "job_id": session_id[:8] if session_id else None, "permission_mode": permission_mode,
                  "platform": self.platform, "launcher": launcher, "stopped_ts": None}
        atomic_write_private(self._path(name, ".json"), json.dumps(_scrub(record), indent=1))
        if audit:
            self._audit("launch", name, session_id, True, warning, shown)
        result = {"ok": True, "name": name, "session_id": session_id, "record": str(self._path(name, ".json"))}
        return dict(result, warning=warning) if warning else result

    def stop(self, name=None, session_id=None, audit=True):
        if name is None and session_id is None:
            raise ParameterRefused("stop needs a name or id")
        rec, label = None, name
        if name is not None:
            _check_name(name)
            rec = self._read_json(self._path(name, ".json"))
        else:
            rec = next((c for c in self._records() if session_id in (c.get("session_id"), c.get("job_id"))), None)
            label = rec["name"] if rec else name
        if rec is None:
            raise ParameterRefused(f"unknown session {name or session_id!r}")
        _check_name(label)
        sid = rec.get("session_id")
        if not sid:
            raise ParameterRefused(f"session {label!r} has no session id")
        p = self.run(["claude", "stop", sid], capture_output=True, text=True)  # never rm, never kill
        if p.returncode != 0:
            if audit:
                self._audit("stop", label, sid, False, f"exit {p.returncode}")
            raise ParameterRefused(f"stop failed for {label!r}: exit {p.returncode}")
        rec["stopped_ts"] = self.now()
        atomic_write_private(self._path(label, ".json"), json.dumps(_scrub(rec), indent=1))
        if audit:
            self._audit("stop", label, sid, True, None)
        return {"ok": True, "name": label, "session_id": sid, "stopped_ts": rec["stopped_ts"]}

    def relaunch(self, name, handoff=None, force=False):
        _load_config(self.config_path)  # fail closed: a relaunch is a launch
        _check_name(name)
        rec = self._read_json(self._path(name, ".json"))
        if rec is None:
            raise ParameterRefused(f"unknown session {name!r}")
        hand = None
        if not force:
            marker = self._read_json(self._path(name, ".ready.json"))
            if marker is None:
                raise ParameterRefused(f"ready marker missing for {name!r}; pass force to relaunch")
            hand = marker.get("handoff")
            try:
                fresh = bool(hand) and Path(hand).is_file() and Path(hand).stat().st_mtime > rec.get("ts", 0)
            except OSError:
                fresh = False
            if not fresh:
                raise ParameterRefused(f"marker handoff {hand!r} missing or not newer than session ts")
        hand = handoff or hand or rec.get("handoff")
        self.stop(name=name, audit=False)  # failure to stop refuses; nothing is launched
        result = self.launch(name=name, cwd=rec["cwd"], model=rec["model"], effort=rec["effort"],
                             mcp_config=rec["mcp"], permission_mode=rec["permission_mode"],
                             prompt_file=rec["prompt_file"], handoff=hand, parent=rec.get("parent"),
                             children=rec.get("children") or [], audit=False)
        self._path(name, ".ready.json").unlink(missing_ok=True)  # marker consumed
        self._audit("relaunch", name, result.get("session_id"), result.get("ok", False), result.get("warning") or result.get("error"))
        return result

    def mark_ready(self, name, handoff, card=None):
        _check_name(name)
        hand = _readable_file(handoff, "handoff")
        path = self._path(name, ".ready.json")
        atomic_write_private(path, json.dumps(_scrub({"handoff": str(hand), "card": card, "ts": self.now()}), indent=1))
        rec = self._read_json(self._path(name, ".json")) or {}
        self._audit("mark_ready", name, rec.get("session_id"), True, None)
        return {"ok": True, "name": name, "marker": str(path)}

    def list(self):
        try:
            cap = _load_config(self.config_path).get("ctx_cap_tokens", 300000)
        except ParameterRefused:
            cap = 300000
        rows = self._agents_rows()
        out = []
        for rec in self._records():
            sid = rec.get("session_id")
            row = next((r for r in rows if r.get("sessionId") == sid), None) if sid else None
            toks = context_tokens(rec.get("cwd"), sid) if sid else None
            out.append({"name": rec.get("name"), "id": sid, "cwd": rec.get("cwd"),
                        "state": (row or {}).get("state") or ("stopped" if rec.get("stopped_ts") else "unknown"),
                        "context_tokens": toks,
                        "context_pct": (toks / cap) if isinstance(toks, int) and cap else None,
                        "ready": self._path(rec.get("name"), ".ready.json").exists()})
        return {"sessions": out}
