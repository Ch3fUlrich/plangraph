"""tests/sessions_support.py — SESSION-SPAWN-MCP shared fakes, fixtures and helpers."""

from __future__ import annotations

import json
import types
from pathlib import Path
import pytest
from plangraph.spawner.sessions import Sessions


UUID1 = "12345678-1234-1234-1234-123456789abc"
UUID2 = "abcdefab-1234-1234-1234-123456789abc"
UUID3 = "cccccccc-1234-1234-1234-123456789abc"
SHORT = "77b56beb"
MODEL, EFFORT, MODE = "mimo/mimo-v2.6-pro", "high", "bypassPermissions"
PROMPT = "do the thing"
DETACHED = 0x8 | 0x200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
BASE = dict(model=MODEL, effort=EFFORT, permission_mode=MODE, prompt=PROMPT)


class FakeRun:
    """The one injectable runner: records argv+kwargs, refuses shells."""

    def __init__(self, stdout=UUID1, launch_rc=0, agents_rows=(), stop_rc=0):
        self.stdout, self.launch_rc = stdout, launch_rc
        self.agents_rows, self.stop_rc = list(agents_rows), stop_rc
        self.calls = []

    def __call__(self, argv, **kw):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv), "argv must be a list of str"
        assert kw.get("shell") in (None, False), "shell=True must never be used"
        self.calls.append((list(argv), kw))
        if self._is_agents(argv):
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(self.agents_rows), stderr="")
        if self._is_stop(argv):
            if self.stop_rc == 0:
                self._finish_rows(argv[2] if len(argv) > 2 else None)
            return types.SimpleNamespace(returncode=self.stop_rc, stdout="", stderr="")
        return types.SimpleNamespace(returncode=self.launch_rc, stdout=self.stdout, stderr="")

    # Dispatch on the tail and accept both the bare name and the resolved
    # win32 .exe head, so the fakes model what production really runs.
    @staticmethod
    def _is_agents(argv):
        return Path(argv[0]).name in ("claude", "claude.exe") and argv[1:3] == ["agents", "--json"]

    @staticmethod
    def _is_stop(argv):
        return Path(argv[0]).name in ("claude", "claude.exe") and argv[1:2] == ["stop"]

    def _finish_rows(self, sid):
        """Answer the confirmation `claude agents` poll a stop triggers: a
        successful `claude stop <sid>` ends that session's row, so the poll
        right after the stop must see the id gone or in a finished state
        (D-3193). Tests modelling an unconfirmed stop override this."""
        for row in self.agents_rows:
            if row.get("sessionId") == sid:
                row["state"] = "finished"

    def launch_calls(self):
        return [argv for argv, _ in self.calls if argv[:2] == ["claude", "--bg"]]


def write_config(path, text):
    """Write a config file pinned owner-only (0600).

    The sessions loader refuses group/world-writable configs; under a umask
    like 002 plain write_text leaves the file group-writable and every action
    would be refused, so the mode is set explicitly after writing."""
    path.write_text(text)
    path.chmod(0o600)
    return path


@pytest.fixture
def site(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "roots"
    proj = root / "proj"
    proj.mkdir(parents=True)
    mcp = tmp_path / "mcp.json"
    write_config(mcp, "{}")
    cfg = tmp_path / "sessions.json"
    # tmp_path is admitted via cwd_roots (file_roots is a cut config key);
    # the tests keep writing their handoff/prompt fixtures under tmp_path.
    write_config(cfg, json.dumps({
        "cwd_roots": [str(root), str(tmp_path)], "models": [MODEL], "efforts": [EFFORT],
        "permission_modes": [MODE], "mcp_configs": [str(mcp)], "ctx_cap_tokens": 300000}))
    return types.SimpleNamespace(tmp_path=tmp_path, home=home, root=root, proj=proj,
                                 mcp=mcp, cfg=cfg, state=tmp_path / "state")


def make(site, run=None, *, platform="linux", now=lambda: 1000.0,
         resolve_budget=15.0, config_path=None, which=None, sleep=lambda _s: None):
    return Sessions(state_root=site.state, config_path=str(config_path or site.cfg),
                    run=run or FakeRun(),
                    now=now, platform=platform, resolve_budget=resolve_budget, which=which,
                    sleep=sleep)


def go(site, s, name="alpha", **over):
    args = dict(BASE, name=name, cwd=str(site.proj), mcp_config=str(site.mcp))
    args.update(over)
    return s.launch(**args)


def _ready(site, run, s):
    """Launch alpha, mark it ready, hand back the marker + record paths."""
    go(site, s)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    return (site.state / "sessions" / "alpha.json"), (site.state / "sessions" / "alpha.ready.json")


def _assert_relaunch_failure_kept(rec_path, marker_path, site, state):
    """A failed relaunch leaves _start's own outcome record on disk (D-3193:
    the pre-slice restore of the pre-relaunch record is cut) and keeps the
    ready marker so a plain retry can relaunch."""
    rec = json.loads(rec_path.read_text())
    assert rec["state"] == state and rec["session_id"] is None
    assert (rec["stopped_ts"] is not None) == (state == "stopped")
    assert marker_path.exists()  # a retry can relaunch
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "relaunch" and line["ok"] is False


def _stopped(site, run, s):
    """Launch alpha, mark it ready, stop it: the record keeps its session id
    with stopped_ts set - the state a retrying relaunch finds."""
    rec_path, marker_path = _ready(site, run, s)
    s.stop(name="alpha")
    return rec_path, marker_path
