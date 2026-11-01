#!/usr/bin/env python3
"""The real pinned mcp-proxy behind the supervisor's default argv (skipped without uvx/network).

Every other mcp_supervise test uses `proxy: false` and a fake HTTP server. This file is
the only place the default command line, the `/mcp` path, `--stateless` and
`--pass-environment` meet the real mcp-proxy, so a wrong flag or path fails here, not at
workstation startup. It needs `uvx` and a one-time download of the pinned version.
"""

import os
import shutil
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

from plangraph.mcp_supervise import supervisor
from plangraph.mcp_supervise.config import load_config
from plangraph.mcp_supervise.health import probe
from plangraph.mcp_supervise.procutil import kill_process_tree, pid_alive
from plangraph.mcp_supervise.supervisor import MCP_PROXY_PINNED_VERSION, build_child_command
from test_mcp_supervise_support import (
    CLI,
    CLI_ENV,
    STDIO_SERVER_SCRIPT,
    WorkdirCase,
    free_port,
    wait_healthy,
    wait_until,
)

UVX = shutil.which("uvx")
PIN_ARGV = ["uvx", f"mcp-proxy=={MCP_PROXY_PINNED_VERSION}"]


def _uvx(*args, timeout=240):
    return subprocess.run([UVX, f"mcp-proxy=={MCP_PROXY_PINNED_VERSION}", *args],
                          capture_output=True, text=True, timeout=timeout, check=False)


@unittest.skipUnless(UVX, "uvx not installed")
class TestRealPinnedProxy(WorkdirCase):
    version_out = ""
    help_out = ""

    @classmethod
    def setUpClass(cls):
        try:
            version, helped = _uvx("--version"), _uvx("--help")
        except (subprocess.TimeoutExpired, OSError) as e:
            raise unittest.SkipTest(f"pinned mcp-proxy not obtainable: {type(e).__name__}")
        if version.returncode != 0 or helped.returncode != 0:
            raise unittest.SkipTest("pinned mcp-proxy not obtainable (offline?)")
        cls.version_out, cls.help_out = version.stdout, helped.stdout

    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.stdio = self.tmp / "stdio_srv.py"
        self.stdio.write_text(STDIO_SERVER_SCRIPT, encoding="utf-8")
        self.starts = self.tmp / "starts.txt"
        self.env = {"STARTS_FILE": str(self.starts), "MARK": "m1"}
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.entry = {"name": "real", "port": self.port, "scope": "host", "env": self.env,
                      "cmd": [sys.executable, str(self.stdio)]}  # proxy defaults to True

    def starts_lines(self):
        return self.starts.read_text(encoding="utf-8").splitlines() if self.starts.exists() else []

    def stdio_pid(self, index=-1):
        return int(self.starts_lines()[index].split()[1].split("=")[1])

    def test_installed_proxy_is_the_pinned_version(self):
        self.assertEqual(self.version_out.split()[-1], MCP_PROXY_PINNED_VERSION)

    def test_every_flag_the_default_argv_uses_exists_in_the_pinned_help(self):
        server = load_config({"servers": [dict(self.entry)]}).servers[0]
        flags = {a for a in build_child_command(server, which=lambda _: None) if a.startswith("--")}
        self.assertEqual(flags, {"--host", "--port", "--stateless", "--pass-environment", "--"})
        for flag in flags - {"--"}:
            self.assertIn(flag, self.help_out)

    def test_default_argv_serves_mcp_probe_and_check_pass(self):
        server = load_config({"servers": [dict(self.entry)]}).servers[0]
        self.assertEqual(build_child_command(server, which=lambda _: None)[:2], PIN_ARGV)
        self.start_supervisor(self.config(dict(self.entry)))
        self.assertTrue(wait_healthy(self.port, tries=900), self.log_text())

        self.assertTrue(probe(self.url, timeout=5.0))
        root = urllib.request.Request(self.url[:-3], data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as miss:
            urllib.request.urlopen(root, timeout=5)
        self.assertEqual(miss.exception.code, 404)

        lines = self.starts_lines()
        self.assertEqual(len(lines), 1, "a probe must not start another stdio session")
        self.assertIn("utf8=1 mark=m1", lines[0], "--pass-environment lost the child env")

        res = subprocess.run(
            CLI + ["check", "--config", str(self.config_path(self.config(dict(self.entry)))),
                   "--name", "real", "--timeout", "120"],
            capture_output=True, text=True, env=CLI_ENV)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(wait_healthy(self.port, tries=300))

    def _orphan_proxy_with_dead_stdio_child(self):
        server = load_config({"servers": [dict(self.entry)]}).servers[0]
        proc = subprocess.Popen(
            build_child_command(server), env={**os.environ, **self.env, "PYTHONUTF8": "1"},
            start_new_session=True)
        self.cleanup_procs.append(proc)
        self.assertTrue(wait_healthy(self.port, tries=900))
        kill_process_tree(self.stdio_pid())
        # The proxy keeps answering `initialize` (measured), only forwarded calls fail.
        self.assertTrue(wait_until(lambda: not probe(self.url, timeout=2.0), tries=100))
        self.assertTrue(probe(self.url, timeout=2.0, deep=False))
        return proc

    def test_deep_probe_flags_a_live_proxy_with_a_dead_server_behind_it(self):
        self._orphan_proxy_with_dead_stdio_child()

    def test_supervisor_replaces_a_live_proxy_whose_server_died(self):
        for name, value in (("INITIAL_BACKOFF", 0.05), ("MAX_BACKOFF", 0.2)):
            patcher = mock.patch.object(supervisor, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self._orphan_proxy_with_dead_stdio_child()
        self.start_supervisor(self.config(dict(self.entry)))

        self.assertTrue(wait_until(lambda: "stale_listener_killed" in self.log_text(), tries=900),
                        self.log_text())
        self.assertTrue(wait_healthy(self.port, tries=900), self.log_text())
        # Each failed bind still starts (and loses) a stdio child before uvicorn gives up.
        self.assertNotEqual(self.stdio_pid(), self.stdio_pid(0))
        self.assertTrue(pid_alive(self.stdio_pid()))
        self.assertTrue(probe(self.url, timeout=5.0))


if __name__ == "__main__":
    unittest.main()
