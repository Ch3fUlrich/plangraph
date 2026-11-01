#!/usr/bin/env python3
"""Tests for plangraph.mcp_supervise."""

import json
import subprocess
import time
import unittest

from plangraph.mcp_supervise.config import ConfigError, load_config
from plangraph.mcp_supervise.health import probe
from plangraph.mcp_supervise.procutil import kill_process_tree
from plangraph.mcp_supervise.render import render_entries, render_json
from plangraph.mcp_supervise.supervisor import calculate_backoff
from test_mcp_supervise_support import (
    CLI,
    CLI_ENV,
    WorkdirCase,
    free_port,
    server_entry,
    wait_healthy,
)


class TestConfigValidation(unittest.TestCase):
    def test_valid_config(self):
        raw = {
            "servers": [
                {
                    "name": "srv1",
                    "port": 9100,
                    "cmd": ["python", "-m", "foo"],
                    "env": {"FOO": "bar"},
                    "scope": "host",
                },
                {
                    "name": "srv2",
                    "port": 9101,
                    "cmd": ["python", "-m", "bar"],
                    "env": {},
                    "scope": "project",
                    "project_dir": "C:/some/repo",
                },
            ]
        }
        cfg = load_config(raw)
        self.assertEqual(len(cfg.servers), 2)
        self.assertEqual(cfg.servers[0].name, "srv1")
        self.assertEqual(cfg.servers[0].port, 9100)
        self.assertEqual(cfg.servers[1].scope, "project")

    def test_duplicate_name(self):
        raw = {
            "servers": [
                {"name": "same", "port": 9100, "cmd": ["python"], "scope": "host"},
                {"name": "same", "port": 9101, "cmd": ["python"], "scope": "host"},
            ]
        }
        with self.assertRaises(ConfigError):
            load_config(raw)

    def test_duplicate_port(self):
        raw = {
            "servers": [
                {"name": "srv1", "port": 9100, "cmd": ["python"], "scope": "host"},
                {"name": "srv2", "port": 9100, "cmd": ["python"], "scope": "host"},
            ]
        }
        with self.assertRaises(ConfigError):
            load_config(raw)

    def test_invalid_ports(self):
        for bad_port in [80, 1023, 65536, "not_int"]:
            raw = {
                "servers": [
                    {"name": "srv", "port": bad_port, "cmd": ["python"], "scope": "host"}
                ]
            }
            with self.assertRaises(ConfigError):
                load_config(raw)

    def test_invalid_host(self):
        raw = {
            "servers": [
                {
                    "name": "srv",
                    "port": 9100,
                    "cmd": ["python"],
                    "scope": "host",
                    "host": "0.0.0.0",
                }
            ]
        }
        with self.assertRaises(ConfigError):
            load_config(raw)

    def test_invalid_scope(self):
        raw = {
            "servers": [
                {"name": "srv", "port": 9100, "cmd": ["python"], "scope": "invalid"}
            ]
        }
        with self.assertRaises(ConfigError):
            load_config(raw)


class TestConfigRules(unittest.TestCase):
    def _one(self, **extra):
        raw = {"name": "srv", "port": 9100, "cmd": ["python"], "scope": "host"}
        raw.update(extra)
        return load_config({"servers": [raw]})

    def test_names_that_escape_the_state_dir_are_rejected(self):
        for bad in ["a/b", "a\\b", "..", ".", "../x", "a b", "-x", "x:y", "", " "]:
            with self.assertRaises(ConfigError, msg=repr(bad)):
                self._one(name=bad)

    def test_ordinary_names_are_accepted(self):
        for ok in ["plangraph-keeper", "serena_1", "a.b", "X9"]:
            self.assertEqual(self._one(name=ok).servers[0].name, ok)

    def test_project_scope_requires_a_project_dir(self):
        for extra in [{}, {"project_dir": None}, {"project_dir": ""}]:
            with self.assertRaises(ConfigError, msg=repr(extra)):
                self._one(scope="project", **extra)
        self._one(scope="project", project_dir="C:/some/repo")

    def test_host_scope_must_not_name_a_project_dir(self):
        with self.assertRaises(ConfigError):
            self._one(scope="host", project_dir="C:/some/repo")


class TestRenderOutput(unittest.TestCase):
    def test_render_shape(self):
        raw = {
            "servers": [
                {"name": "srv1", "port": 9100, "cmd": ["python"], "scope": "host"},
                {"name": "srv2", "port": 9101, "cmd": ["python"], "scope": "project",
                 "project_dir": "C:/some/repo"},
            ]
        }
        cfg = load_config(raw)
        entries = render_entries(cfg)
        self.assertEqual(
            entries,
            {
                "srv1": {"type": "http", "url": "http://127.0.0.1:9100/mcp"},
                "srv2": {"type": "http", "url": "http://127.0.0.1:9101/mcp"},
            },
        )
        rendered_str = render_json(cfg)
        parsed = json.loads(rendered_str)
        self.assertIn("srv1", parsed)
        self.assertEqual(parsed["srv1"]["type"], "http")


class TestBackoffCalculation(unittest.TestCase):
    def test_backoff_progression_and_cap(self):
        delays = []
        b = 1.0
        for _ in range(8):
            delays.append(b)
            b = calculate_backoff(b)
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0])


class TestHealthProbe(WorkdirCase):
    def test_probe_success_and_failure(self):
        port = free_port()
        self.spawn_orphan(port)
        self.assertTrue(wait_healthy(port))
        self.assertFalse(probe(f"http://127.0.0.1:{free_port()}/mcp", timeout=0.5))

    def test_deep_probe_sees_a_live_proxy_whose_server_behind_it_is_dead(self):
        # mcp-proxy 0.13.0 answers `initialize` itself and keeps doing so after its
        # stdio child died; only a forwarded request (tools/list) fails.
        port = free_port()
        self.spawn_orphan(port, "stale")
        url = f"http://127.0.0.1:{port}/mcp"
        self.assertTrue(wait_until_probe(url, deep=False))
        self.assertFalse(probe(url, timeout=1.0))
        self.assertTrue(probe(url, timeout=1.0, deep=False))


def wait_until_probe(url: str, deep: bool) -> bool:
    for _ in range(50):
        if probe(url, timeout=0.5, deep=deep):
            return True
        time.sleep(0.1)
    return False


class TestSupervisorAndCheck(WorkdirCase):
    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.port2 = free_port()
        self.secret = "hygiene-fixture-9"

    def test_supervisor_restart_and_log_hygiene(self):
        raw = self.config(server_entry(
            "test-server", self.port, self.script, env={"SECRET_KEY": self.secret}))
        self.start_supervisor(raw)
        self.assertTrue(wait_healthy(self.port), "Server failed to start and answer probe")

        pid = self.pid_of("test-server")
        self.assertIsNotNone(pid)
        kill_process_tree(pid)

        self.assertTrue(wait_healthy(self.port, tries=60), "Server failed to restart after kill")

        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertNotIn(self.secret, log_content, "Secret was leaked into the supervisor log!")
        self.assertIn("name=test-server", log_content)
        fields = [dict(kv.split("=", 1) for kv in line.split()[1:])
                  for line in log_content.splitlines()]
        start = next(f for f in fields if f["action"] == "start")
        self.assertEqual(start["name"], "test-server")
        self.assertEqual(int(start["pid"]), pid, "start line must carry the child's real pid")
        self.assertEqual(start["rc"], "None", "a start has no exit code yet")
        exited = next(f for f in fields if f["action"] == "exit")
        self.assertEqual(int(exited["pid"]), pid)
        int(exited["rc"])  # numeric exit code of the killed child
        restart = next(f for f in fields if f["action"] == "restart")
        self.assertGreater(int(restart["pid"]), 0)
        self.assertNotEqual(int(restart["pid"]), pid)
        self.assertTrue(all({"name", "pid", "action", "rc", "reason"} <= f.keys() for f in fields))

    def test_check_cli_exit_codes(self):
        raw = self.config(server_entry("test-check", self.port2, self.script))
        config_path = self.config_path(raw)

        res = subprocess.run(
            CLI + ["check", "--config", str(config_path), "--name", "nonexistent"],
            capture_output=True, text=True, env=CLI_ENV)
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("ModuleNotFoundError", res.stderr)
        self.assertIn("nonexistent", res.stdout + res.stderr)

        self.start_supervisor(raw)
        self.assertTrue(wait_healthy(self.port2))

        res = subprocess.run(
            CLI + ["check", "--config", str(config_path), "--name", "test-check"],
            capture_output=True, text=True, env=CLI_ENV)
        self.assertEqual(res.returncode, 0, f"Check failed: {res.stderr}")
        self.assertIn("healthy in", res.stdout.lower())


if __name__ == "__main__":
    unittest.main()
