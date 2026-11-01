#!/usr/bin/env python3
"""Command line, log, shipped-example, docs and launcher tests for mcp_supervise."""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from plangraph.mcp_supervise import supervisor
from plangraph.mcp_supervise.config import load_config
from plangraph.mcp_supervise.render import render_entries
from plangraph.mcp_supervise.supervisor import (
    MCP_PROXY_PINNED_VERSION,
    Supervisor,
    build_child_command,
)
from test_mcp_supervise_support import ROOT, free_port

EXAMPLE = ROOT / "examples" / "mcp-supervise.workstation.json"
DOCS = ROOT / "docs" / "mcp-supervise.md"
PS1 = ROOT / "tools" / "mcp-supervise.ps1"
PIN = "0.13.0"


def _server(port=9100, **extra):
    raw = {"name": "s", "port": port, "cmd": ["python", "-m", "foo", "--x"], "scope": "host"}
    raw.update(extra)
    return load_config({"servers": [raw]}).servers[0]


def _flags(port):
    return ["--host", "127.0.0.1", "--port", str(port), "--stateless",
            "--pass-environment", "--"]


class TestBuildChildCommand(unittest.TestCase):
    def test_pin_constant(self):
        self.assertEqual(MCP_PROXY_PINNED_VERSION, PIN)

    def test_default_is_pinned_uvx_form_exact_argv(self):
        which = {"uvx": "/bin/uvx"}.get
        argv = build_child_command(_server(9123), which=which)
        self.assertEqual(
            argv,
            ["/bin/uvx", f"mcp-proxy=={PIN}"] + _flags(9123) + ["python", "-m", "foo", "--x"])

    def test_uvx_missing_falls_back_to_bare_name(self):
        argv = build_child_command(_server(), which=lambda _: None)
        self.assertEqual(argv[:2], ["uvx", f"mcp-proxy=={PIN}"])

    def test_path_proxy_with_other_version_does_not_override_pin(self):
        which = {"uvx": "/bin/uvx", "mcp-proxy": "/bin/mcp-proxy"}.get
        argv = build_child_command(_server(), which=which, proxy_version=lambda _: "0.12.0")
        self.assertEqual(argv[:2], ["/bin/uvx", f"mcp-proxy=={PIN}"])

    def test_path_proxy_with_unknown_version_does_not_override_pin(self):
        which = {"uvx": "/bin/uvx", "mcp-proxy": "/bin/mcp-proxy"}.get
        argv = build_child_command(_server(), which=which, proxy_version=lambda _: None)
        self.assertEqual(argv[:2], ["/bin/uvx", f"mcp-proxy=={PIN}"])

    def test_path_proxy_with_pinned_version_is_used(self):
        which = {"uvx": "/bin/uvx", "mcp-proxy": "/bin/mcp-proxy"}.get
        asked = []
        argv = build_child_command(
            _server(9124), which=which, proxy_version=lambda p: asked.append(p) or PIN)
        self.assertEqual(asked, ["/bin/mcp-proxy"])
        self.assertEqual(argv, ["/bin/mcp-proxy"] + _flags(9124) + ["python", "-m", "foo", "--x"])

    def test_proxy_false_runs_cmd_verbatim(self):
        self.assertEqual(build_child_command(_server(proxy=False)), ["python", "-m", "foo", "--x"])


class TestLogging(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-test-log-"))
        self.log = self.tmp / "t.log"

    def _sup(self, **entry):
        raw = {"name": "a", "port": free_port(),
               "cmd": [sys.executable, "-c", "pass"], "scope": "host", "proxy": False}
        raw.update(entry)
        cfg = load_config({"log_file": str(self.log), "state_dir": str(self.tmp / "st"),
                           "servers": [raw]})
        return Supervisor(cfg, poll_interval=0.01)

    def test_spawn_failed_logs_exception_class_only(self):
        sup = self._sup()
        boom = OSError("argv had token=sk-SECRET-123")
        with mock.patch.object(supervisor.subprocess, "Popen", side_effect=boom):
            sup._start_child(sup.trackers["a"], reason="t")
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("spawn_failed: OSError", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("token", text)

    def test_restart_log_shows_delay_used_not_the_next_one(self):
        with mock.patch.object(supervisor, "INITIAL_BACKOFF", 0.05), \
                mock.patch.object(supervisor, "MAX_BACKOFF", 0.4):
            sup = self._sup()
            stop = sup.stop_event
            t = threading.Thread(target=sup.run, daemon=True)
            t.start()
            deadline = time.time() + 15
            while time.time() < deadline:
                if self.log.exists() and self.log.read_text(encoding="utf-8").count(
                        "action=restart") >= 3:
                    break
                time.sleep(0.05)
            stop.set()
            t.join(timeout=5)
        reasons = re.findall(r"action=restart rc=None reason=(\S+)",
                             self.log.read_text(encoding="utf-8"))
        self.assertEqual(reasons[:3],
                         ["backoff_restart_0.05s", "backoff_restart_0.1s", "backoff_restart_0.2s"])


class TestShippedExample(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def test_example_loads(self):
        load_config(str(EXAMPLE))

    def test_serena_entries_use_the_real_invocation_with_explicit_project(self):
        serena = [s for s in self.raw["servers"] if s["name"].startswith("serena")]
        self.assertGreaterEqual(len(serena), 2)
        for s in serena:
            self.assertEqual(
                s["cmd"][:7],
                ["uvx", "--from", "serena-agent==1.7.0", "serena", "start-mcp-server",
                 "--context", "claude-code"], s["name"])
            self.assertEqual(s["cmd"][7], "--project", s["name"])
            self.assertEqual(s["cmd"][8], s["project_dir"], s["name"])
            self.assertEqual(s["scope"], "project")
        self.assertEqual(len({s["cmd"][8] for s in serena}), len(serena))
        self.assertNotIn("serena-mcp", EXAMPLE.read_text(encoding="utf-8"))


class TestDocs(unittest.TestCase):
    def test_render_example_matches_shipped_config(self):
        text = DOCS.read_text(encoding="utf-8")
        block = re.search(r"Output:\s*```json\n(.*?)```", text, re.S)
        self.assertIsNotNone(block, "docs lack the render Output block")
        self.assertEqual(json.loads(block.group(1)),
                         render_entries(load_config(str(EXAMPLE))))

    def test_docs_record_the_measurement(self):
        text = DOCS.read_text(encoding="utf-8")
        self.assertIn("2026-11-01", text)
        self.assertIn(f"mcp-proxy=={PIN}", text)
        self.assertIn("--project", text)
        self.assertLessEqual(len(text.splitlines()), 150)


PWSH = shutil.which("pwsh")


@unittest.skipUnless(PWSH, "pwsh not installed")
class TestLauncher(unittest.TestCase):
    def _launch(self, out, *args):
        return subprocess.Popen(
            [PWSH, "-NoProfile", "-File", str(PS1), "-Config", str(out.parent / "missing.json"),
             *args], stdout=out.open("w"), stderr=subprocess.STDOUT, start_new_session=True)

    def _wait_for(self, out, text, secs=60):
        end = time.time() + secs
        while time.time() < end:
            if text in out.read_text(encoding="utf-8", errors="replace"):
                return True
            time.sleep(0.2)
        return False

    def test_second_instance_exits_zero_with_message(self):
        d = Path(tempfile.mkdtemp(prefix="mcp-test-ps1-"))
        name = f"Local\\plangraph-mcp-supervise-test-{free_port()}"
        first_out, second_out = d / "first.txt", d / "second.txt"
        first = self._launch(first_out, "-MutexName", name, "-RestartDelay", "30")
        try:
            self.assertTrue(self._wait_for(first_out, "restarting in 30"))
            second = self._launch(second_out, "-MutexName", name)
            self.assertEqual(second.wait(timeout=60), 0)
            self.assertIn("already running", second_out.read_text(encoding="utf-8"))
            self.assertIsNone(first.poll())
        finally:
            first.kill()
            first.wait(timeout=10)

    def test_repeated_immediate_failures_back_off(self):
        d = Path(tempfile.mkdtemp(prefix="mcp-test-ps1-"))
        out = d / "out.txt"
        name = f"Local\\plangraph-mcp-supervise-test-{free_port()}"
        proc = self._launch(out, "-MutexName", name, "-RestartDelay", "0",
                            "-BackoffDelay", "777", "-FailLimit", "3")
        try:
            self.assertTrue(self._wait_for(out, "restarting in 777"))
            text = out.read_text(encoding="utf-8", errors="replace")
            self.assertEqual(text.count("restarting in 0 seconds"), 2, text)
        finally:
            proc.kill()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
