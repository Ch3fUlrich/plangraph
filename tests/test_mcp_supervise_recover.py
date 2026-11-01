#!/usr/bin/env python3
"""Recovery tests for mcp_supervise: kill results, pid identity, adoption, stale listeners."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from plangraph.mcp_supervise import procutil, supervisor
from plangraph.mcp_supervise.config import load_config
from plangraph.mcp_supervise.portowner import listener_pid
from plangraph.mcp_supervise.procutil import (
    kill_process_tree,
    pid_alive,
    pid_matches,
    process_start_time,
    read_pid_record,
    write_pid_file,
)
from test_mcp_supervise_support import (
    CLI,
    CLI_ENV,
    WorkdirCase,
    free_port,
    server_entry,
    wait_healthy,
    wait_until,
)


class TestKillResult(unittest.TestCase):
    def _taskkill(self, rc, stderr, alive):
        done = subprocess.CompletedProcess([], rc, stdout="", stderr=stderr)
        msgs = []
        with mock.patch.object(procutil.sys, "platform", "win32"), \
                mock.patch.object(procutil.subprocess, "run", return_value=done), \
                mock.patch.object(procutil, "pid_alive", return_value=alive):
            return kill_process_tree(4242, log=msgs.append), msgs

    def test_failed_taskkill_is_reported_not_swallowed(self):
        ok, msgs = self._taskkill(1, "ERROR: Access is denied.", alive=True)
        self.assertFalse(ok)
        self.assertEqual(len(msgs), 1)
        self.assertIn("rc=1", msgs[0])
        self.assertIn("Access is denied", msgs[0])

    def test_success_and_already_gone_are_ok(self):
        self.assertEqual(self._taskkill(0, "", alive=False), (True, []))
        ok, _ = self._taskkill(128, "ERROR: not found", alive=False)
        self.assertTrue(ok)

    def test_real_kill_returns_true_and_the_process_dies(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                start_new_session=True)
        self.assertTrue(kill_process_tree(proc.pid))
        proc.wait(timeout=10)
        self.assertFalse(pid_alive(proc.pid))


@unittest.skipUnless(sys.platform == "win32", "exit code 259 is the Windows STILL_ACTIVE clash")
class TestPidAliveStillActive(unittest.TestCase):
    def test_a_process_that_exited_with_259_is_dead(self):
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(259)"])
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, 259)
        self.assertFalse(pid_alive(proc.pid))


class TestPidIdentity(WorkdirCase):
    def test_write_records_the_start_time_alongside_the_pid(self):
        sleeper = self.spawn_sleeper()
        write_pid_file(self.state_dir, "x", sleeper.pid)
        pid, start = read_pid_record(self.state_dir / "x.pid")
        self.assertEqual(pid, sleeper.pid)
        self.assertIsNotNone(start)
        self.assertEqual(start, process_start_time(sleeper.pid))

    def test_pid_matches_rejects_a_recycled_pid(self):
        start = process_start_time(os.getpid())
        self.assertTrue(pid_matches(os.getpid(), start))
        self.assertFalse(pid_matches(os.getpid(), "not-the-start-time"))
        self.assertTrue(pid_matches(os.getpid(), None), "unknown start time is unverifiable")

    def test_legacy_bare_pid_file_still_reads(self):
        self.state_dir.mkdir()
        (self.state_dir / "x.pid").write_text("123", encoding="utf-8")
        self.assertEqual(read_pid_record(self.state_dir / "x.pid"), (123, None))

    def test_check_never_kills_a_recycled_pid(self):
        port = free_port()
        bystander = self.spawn_sleeper()

        self.state_dir.mkdir()
        (self.state_dir / "a.pid").write_text(f"{bystander.pid} 1", encoding="utf-8")
        cfg = self.config(server_entry("a", port, self.script))
        res = subprocess.run(
            CLI + ["check", "--config", str(self.config_path(cfg)), "--name", "a",
                   "--timeout", "2"],
            capture_output=True, text=True, env=CLI_ENV)
        self.assertEqual(res.returncode, 1)
        self.assertIn("recycled", res.stdout + res.stderr)
        self.assertIsNone(bystander.poll(), "an unrelated process was killed")


class TestListenerPid(WorkdirCase):
    def test_finds_the_process_listening_on_a_port_and_nothing_else(self):
        port = free_port()
        self.assertIsNone(listener_pid("127.0.0.1", port))
        self.spawn_orphan(port)
        self.assertTrue(wait_healthy(port))
        pid = listener_pid("127.0.0.1", port)
        self.assertIsNotNone(pid)
        self.assertTrue(pid_alive(pid))
        self.assertNotEqual(pid, os.getpid())


class TestAdoptionRecordsPid(WorkdirCase):
    def _adopt(self, port):
        sup = self.start_supervisor(self.config(server_entry("a", port, self.script)))
        self.assertTrue(wait_until(lambda: "adopted_existing" in self.log_text()))
        return sup

    def test_adopted_server_without_a_pid_file_gets_one(self):
        port = free_port()
        self.spawn_orphan(port)
        self.assertTrue(wait_healthy(port))
        self._adopt(port)
        self.assertEqual(self.pid_of("a"), listener_pid("127.0.0.1", port))

    def test_stale_pid_file_is_replaced_by_the_listener(self):
        port = free_port()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        write_pid_file(self.state_dir, "a", dead.pid)
        self.spawn_orphan(port)
        self.assertTrue(wait_healthy(port))
        self._adopt(port)
        self.assertEqual(self.pid_of("a"), listener_pid("127.0.0.1", port))

    def test_check_works_against_an_adopted_server_that_had_no_pid_file(self):
        port = free_port()
        orphan = self.spawn_orphan(port)
        self.assertTrue(wait_healthy(port))
        raw = self.config(server_entry("a", port, self.script))
        self.spawn_supervisor(raw)
        self.assertTrue(wait_until(lambda: self.pid_of("a") is not None))
        old = self.pid_of("a")
        res = subprocess.run(
            CLI + ["check", "--config", str(self.config_path(raw)), "--name", "a",
                   "--timeout", "40"],
            capture_output=True, text=True, env=CLI_ENV)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(wait_until(lambda: orphan.poll() is not None))
        self.assertFalse(pid_alive(old))
        self.assertNotEqual(self.pid_of("a"), old)


class TestStaleListener(WorkdirCase):
    def setUp(self):
        super().setUp()
        for name, value in (("INITIAL_BACKOFF", 0.05), ("MAX_BACKOFF", 0.2)):
            patcher = mock.patch.object(supervisor, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_listener_that_fails_the_deep_probe_is_replaced(self):
        port = free_port()
        stale = self.spawn_orphan(port, "stale")
        self.assertTrue(wait_until(lambda: listener_pid("127.0.0.1", port) is not None))
        self.start_supervisor(self.config(server_entry("a", port, self.script)))

        self.assertTrue(wait_until(lambda: "stale_listener_killed" in self.log_text(), tries=200))
        self.assertTrue(wait_healthy(port, tries=100), self.log_text())
        stale.wait(timeout=10)
        self.assertNotIn("adopted_existing", self.log_text())

    def test_a_healthy_listener_is_never_killed(self):
        port = free_port()
        healthy = self.spawn_orphan(port)
        self.assertTrue(wait_healthy(port))
        self.start_supervisor(self.config(server_entry("a", port, self.script)))
        self.assertTrue(wait_until(lambda: "adopted_existing" in self.log_text()))
        self.assertNotIn("stale_listener", self.log_text())
        self.assertIsNone(healthy.poll())


class TestProjectDirIsEnforced(WorkdirCase):
    def test_missing_project_dir_refuses_to_spawn_and_logs(self):
        raw = self.config(server_entry(
            "a", free_port(), self.script, scope="project",
            project_dir=str(self.tmp / "does-not-exist")))
        sup = supervisor.Supervisor(load_config(raw))
        with mock.patch.object(supervisor.subprocess, "Popen") as popen, \
                mock.patch.object(supervisor, "write_pid_file"):
            sup._start_child(sup.trackers["a"], reason="t")
        popen.assert_not_called()
        self.assertIsNone(sup.trackers["a"].proc)
        self.assertGreater(sup.trackers["a"].next_restart_time, 0)
        self.assertIn("project_dir_missing", self.log_text())

    def test_existing_project_dir_becomes_the_cwd(self):
        project = self.tmp / "proj"
        project.mkdir()
        raw = self.config(server_entry(
            "a", free_port(), self.script, scope="project", project_dir=str(project)))
        sup = supervisor.Supervisor(load_config(raw))
        with mock.patch.object(supervisor.subprocess, "Popen") as popen, \
                mock.patch.object(supervisor, "write_pid_file"):
            popen.return_value.pid = 4242
            sup._start_child(sup.trackers["a"], reason="t")
        self.assertEqual(Path(popen.call_args.kwargs["cwd"]), project)


class TestProxyResolvedOnce(WorkdirCase):
    def test_version_probe_runs_once_not_per_restart(self):
        raw = self.config(server_entry("a", free_port(), self.script, proxy=True))
        asked = []

        def version(path):
            asked.append(path)
            return supervisor.MCP_PROXY_PINNED_VERSION

        sup = supervisor.Supervisor(
            load_config(raw), which={"mcp-proxy": "/bin/mcp-proxy"}.get, proxy_version=version)
        with mock.patch.object(supervisor.subprocess, "Popen") as popen, \
                mock.patch.object(supervisor, "write_pid_file"):
            popen.return_value.pid = 4242
            for _ in range(3):
                sup._start_child(sup.trackers["a"], reason="t")
        self.assertEqual(asked, ["/bin/mcp-proxy"])
        self.assertEqual(popen.call_count, 3)
        self.assertEqual({c.args[0][0] for c in popen.call_args_list}, {"/bin/mcp-proxy"})


if __name__ == "__main__":
    unittest.main()
