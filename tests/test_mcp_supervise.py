#!/usr/bin/env python3
"""Tests for plangraph.mcp_supervise."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from plangraph.mcp_supervise.config import ConfigError, load_config
from plangraph.mcp_supervise.health import probe
from plangraph.mcp_supervise.render import render_entries, render_json
from plangraph.mcp_supervise.supervisor import (
    calculate_backoff,
    kill_process_tree,
    run_supervisor,
)
from test_mcp_supervise_support import CLI_ENV as _CLI_ENV
from test_mcp_supervise_support import FAKE_SERVER_SCRIPT, free_port


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


class TestRenderOutput(unittest.TestCase):
    def test_render_shape(self):
        raw = {
            "servers": [
                {"name": "srv1", "port": 9100, "cmd": ["python"], "scope": "host"},
                {"name": "srv2", "port": 9101, "cmd": ["python"], "scope": "project"},
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


class TestHealthProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-test-probe-"))
        self.fake_script = self.tmp / "fake_srv.py"
        self.fake_script.write_text(FAKE_SERVER_SCRIPT, encoding="utf-8")
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, str(self.fake_script), str(self.port)]
        )

    def tearDown(self):
        kill_process_tree(self.proc.pid)
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass

    def test_probe_success_and_failure(self):
        url = f"http://127.0.0.1:{self.port}/mcp"
        healthy = False
        for _ in range(30):
            if probe(url, timeout=0.5):
                healthy = True
                break
            time.sleep(0.1)
        self.assertTrue(healthy)
        # Non-existent endpoint or port
        self.assertFalse(probe(f"http://127.0.0.1:{free_port()}/mcp", timeout=0.5))


class TestSupervisorAndCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-test-sup-"))
        self.fake_script = self.tmp / "fake_srv.py"
        self.fake_script.write_text(FAKE_SERVER_SCRIPT, encoding="utf-8")
        self.log_file = self.tmp / "test.log"
        self.state_dir = self.tmp / "state"
        self.port = free_port()
        self.port2 = free_port()
        self.secret = "SUPER_SECRET_TOKEN_DO_NOT_LOG"

    def tearDown(self):
        # Clean up any child processes
        pass

    def test_supervisor_restart_and_log_hygiene(self):
        raw = {
            "log_file": str(self.log_file),
            "state_dir": str(self.state_dir),
            "servers": [
                {
                    "name": "test-server",
                    "port": self.port,
                    "cmd": [sys.executable, str(self.fake_script), str(self.port)],
                    "env": {"SECRET_KEY": self.secret},
                    "scope": "host",
                    "proxy": False,
                }
            ],
        }
        cfg = load_config(raw)
        stop_event = threading.Event()
        sup_thread = threading.Thread(
            target=run_supervisor, args=(cfg, stop_event, 0.05), daemon=True
        )
        sup_thread.start()

        url = f"http://127.0.0.1:{self.port}/mcp"
        healthy = False
        for _ in range(50):
            if probe(url, timeout=0.5):
                healthy = True
                break
            time.sleep(0.1)
        self.assertTrue(healthy, "Server failed to start and answer probe")

        # Read PID of the child process
        pid_file = self.state_dir / "test-server.pid"
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text(encoding="utf-8").strip())

        # Kill the child process
        kill_process_tree(pid)

        # Child should be restarted by supervisor and probe healthy again
        restarted_healthy = False
        for _ in range(60):
            if probe(url, timeout=0.5):
                restarted_healthy = True
                break
            time.sleep(0.1)
        self.assertTrue(restarted_healthy, "Server failed to restart after kill")

        # Stop supervisor
        stop_event.set()
        sup_thread.join(timeout=5)

        # Verify log hygiene
        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertNotIn(
            self.secret, log_content, "Secret was leaked into the supervisor log!"
        )
        self.assertIn("name=test-server", log_content)
        self.assertIn("pid=", log_content)
        self.assertIn("action=start", log_content)
        self.assertIn("rc=", log_content)

    def test_check_cli_exit_codes(self):
        config_path = self.tmp / "config.json"
        raw = {
            "log_file": str(self.log_file),
            "state_dir": str(self.state_dir),
            "servers": [
                {
                    "name": "test-check",
                    "port": self.port2,
                    "cmd": [sys.executable, str(self.fake_script), str(self.port2)],
                    "env": {},
                    "scope": "host",
                    "proxy": False,
                }
            ],
        }
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        cfg = load_config(raw)

        # Check against unknown server should exit 1
        res = subprocess.run(
            [
                sys.executable,
                "-m",
                "plangraph.mcp_supervise",
                "check",
                "--config",
                str(config_path),
                "--name",
                "nonexistent",
            ],
            capture_output=True,
            text=True,
            env=_CLI_ENV,
        )
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("ModuleNotFoundError", res.stderr)
        self.assertIn("nonexistent", res.stdout + res.stderr)

        # Now start supervisor in thread
        stop_event = threading.Event()
        sup_thread = threading.Thread(
            target=run_supervisor, args=(cfg, stop_event, 0.05), daemon=True
        )
        sup_thread.start()

        url = f"http://127.0.0.1:{self.port2}/mcp"
        for _ in range(50):
            if probe(url, timeout=0.5):
                break
            time.sleep(0.1)

        try:
            # Check command should kill child, wait for healthy, print seconds, and exit 0
            res = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "plangraph.mcp_supervise",
                    "check",
                    "--config",
                    str(config_path),
                    "--name",
                    "test-check",
                ],
                capture_output=True,
                text=True,
                env=_CLI_ENV,
            )
            self.assertEqual(res.returncode, 0, f"Check failed: {res.stderr}")
            self.assertIn("healthy in", res.stdout.lower())
        finally:
            stop_event.set()
            sup_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
