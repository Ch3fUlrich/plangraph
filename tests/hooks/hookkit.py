"""Shared harness for the tests/hooks suites that drive tools/hooks scripts the way Claude Code does.

A hook is a command: the hook JSON on stdin, optional JSON on stdout, an exit code. Every run here gets a
scrubbed environment (no AUTOOS_CTX_*, FLEET_* or routing variable of the machine running the tests) and a
throwaway HOME plus XDG state dir, so nothing reads or writes the live machine (D-852 hermetic tests).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "tools" / "hooks"

SID = "11111111-2222-3333-4444-555555555555"

# Variables the hooks read; a run starts without any of them (prefix match).
_SCRUBBED = ("AUTOOS_CTX_", "AUTOOS_ROOT", "FLEET_", "ROUTING_STATE_DIR", "CLAUDE_CODE_SESSION_ID",
             "XDG_STATE_HOME", "DEST")


def clean_env(home, **extra):
    """The machine's environment minus the hook variables, rooted at `home`; a None in `extra` unsets a name."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUBBED)}
    env["HOME"] = str(home)  # the default state dirs must not hit the real home
    if os.name == "nt":
        env["USERPROFILE"] = str(home)  # Windows Path.home() reads USERPROFILE
    env["XDG_STATE_HOME"] = str(Path(home) / "state")
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def run_script(script, stdin="", env=None, args=(), cwd=None, timeout=30):
    """Run a python script as a child process; `stdin` may be a str or a JSON-able object."""
    raw = stdin if isinstance(stdin, str) else json.dumps(stdin)
    return subprocess.run([sys.executable, str(script), *args], input=raw, capture_output=True, text=True,
                          encoding="utf-8", env=env, cwd=cwd, timeout=timeout)


def load_module(name):
    """Import tools/hooks/<name>.py by path under a private module name, leaving sys.path as it was."""
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("_hooktest_" + name, HOOKS / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


def usage_line(inp=0, read=0, create=0, out=10):
    """One transcript record in the shape Claude Code writes: an assistant message with a usage block."""
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": inp, "cache_read_input_tokens": read,
        "cache_creation_input_tokens": create, "output_tokens": out}}})


class HookCase(unittest.TestCase):
    """A temp dir per test with HOME, the state dir and the transcript path laid out the way the hooks expect."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.tmp / "state"
        self.transcript = self.tmp / "t.jsonl"

    def env(self, **extra):
        return clean_env(self.home, **{"XDG_STATE_HOME": self.state, **extra})

    def patch_env(self, **values):
        """Patch this process's environment for the test (None unsets); restored on cleanup."""
        patcher = mock.patch.dict(os.environ, {k: str(v) for k, v in values.items() if v is not None})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)

    def write(self, *lines):
        self.transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_hook(self, script, payload="", args=(), **env):
        return run_script(script, payload, env=self.env(**env), args=args, cwd=str(self.tmp))
