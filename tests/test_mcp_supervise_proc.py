#!/usr/bin/env python3
"""Process-level tests for plangraph.mcp_supervise: isolation, adoption, pid-file `check`."""

import os
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
from plangraph.mcp_supervise.procutil import (
    SUPERVISOR_PID_NAME,
    pid_alive,
    read_pid_file,
    write_pid_file,
)
from plangraph.mcp_supervise.supervisor import kill_process_tree, run_supervisor
from test_mcp_supervise_support import (
    CLI,
    CLI_ENV,
    WorkdirCase,
    free_port,
    server_entry,
    wait_healthy,
    wait_until as _wait,
)


class TestChildIsolation(WorkdirCase):
    def test_children_start_in_their_own_session(self):
        cfg = load_config(self.config(server_entry("a", free_port(), self.script)))
        sup = supervisor.Supervisor(cfg)
        with mock.patch.object(supervisor.subprocess, "Popen") as popen, \
                mock.patch.object(supervisor, "write_pid_file"):
            popen.return_value.pid = 4242
            sup._start_child(sup.trackers["a"], reason="t")
        self.assertIs(popen.call_args.kwargs.get("start_new_session"), True)
        self.assertIs(popen.call_args.kwargs.get("shell"), False)

    def test_killing_one_child_spares_supervisor_and_other_child(self):
        pa, pb = free_port(), free_port()
        sup = self.spawn_supervisor(self.config(
            server_entry("a", pa, self.script), server_entry("b", pb, self.script)))
        self.assertTrue(wait_healthy(pa) and wait_healthy(pb))
        a_pid, b_pid = self.pid_of("a"), self.pid_of("b")

        kill_process_tree(a_pid)

        self.assertTrue(_wait(lambda: self.pid_of("a") not in (None, a_pid)
                              and wait_healthy(pa, tries=1)))
        self.assertIsNone(sup.poll(), "supervisor died with the killed child")
        self.assertEqual(self.pid_of("b"), b_pid)
        self.assertTrue(pid_alive(b_pid))
        self.assertTrue(wait_healthy(pb, tries=1))


class TestPidFile(unittest.TestCase):
    def test_write_is_atomic_and_leaves_no_temp_file(self):
        d = Path(tempfile.mkdtemp(prefix="mcp-test-pid-"))
        write_pid_file(d, "x", 123)
        write_pid_file(d, "x", 456)
        self.assertEqual(read_pid_file(d / "x.pid"), 456)
        self.assertEqual([p.name for p in d.iterdir()], ["x.pid"])
        write_pid_file(d, "x", None)
        self.assertEqual(list(d.iterdir()), [])

    def test_read_rejects_garbage(self):
        d = Path(tempfile.mkdtemp(prefix="mcp-test-pid-"))
        (d / "x.pid").write_text("not-a-pid", encoding="utf-8")
        self.assertIsNone(read_pid_file(d / "x.pid"))
        self.assertIsNone(read_pid_file(d / "missing.pid"))

    def test_pid_alive(self):
        self.assertTrue(pid_alive(os.getpid()))
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(pid_alive(proc.pid))
        self.assertFalse(pid_alive(0))


class TestAdoption(WorkdirCase):
    def test_existing_server_is_adopted_not_respawned(self):
        port = free_port()
        orphan = subprocess.Popen([sys.executable, str(self.script), str(port)])
        self.cleanup_procs.append(orphan)
        self.assertTrue(wait_healthy(port))
        cfg = load_config(self.config(server_entry("a", port, self.script)))
        stop = threading.Event()
        t = threading.Thread(target=run_supervisor, args=(cfg, stop, 0.05), daemon=True)
        t.start()
        try:
            self.assertTrue(_wait(lambda: "adopted_existing" in self.log_text()))
            time.sleep(0.5)
            self.assertNotIn("action=start", self.log_text())
            self.assertIsNone(orphan.poll())

            kill_process_tree(orphan.pid)
            orphan.wait(timeout=5)
            self.assertTrue(_wait(lambda: "action=start" in self.log_text(), tries=100))
            self.assertTrue(wait_healthy(port))
            new_pid = self.pid_of("a")
            self.assertTrue(new_pid and new_pid != orphan.pid)
        finally:
            stop.set()
            t.join(timeout=5)

    def test_relaunched_supervisor_adopts_surviving_children(self):
        port = free_port()
        raw = self.config(server_entry("a", port, self.script))
        first = self.spawn_supervisor(raw)
        self.assertTrue(wait_healthy(port))
        orphan_pid = self.pid_of("a")
        first.kill()
        first.wait(timeout=5)
        self.assertTrue(pid_alive(orphan_pid), "child must outlive a supervisor crash")

        second = self.spawn_supervisor(raw)
        self.assertTrue(_wait(lambda: "adopted_existing" in self.log_text()))
        time.sleep(1.0)
        self.assertIsNone(second.poll(), "relaunched supervisor crash-looped on the bound port")

        res = subprocess.run(
            CLI + ["check", "--config", str(self.config_path(raw)), "--name", "a",
                   "--timeout", "30"],
            capture_output=True, text=True, env=CLI_ENV)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse(pid_alive(orphan_pid), "check must kill the pid from the pid file")
        self.assertNotEqual(self.pid_of("a"), orphan_pid)


class TestCheckGuards(WorkdirCase):
    def _check(self, raw, timeout="3", *extra):
        return subprocess.run(
            CLI + ["check", "--config", str(self.config_path(raw)), "--name", "a",
                   "--timeout", timeout, *extra],
            capture_output=True, text=True, env=CLI_ENV)

    def _decoy_server(self):
        port = free_port()
        server = subprocess.Popen([sys.executable, str(self.script), str(port)])
        decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.cleanup_procs += [server, decoy]
        self.assertTrue(wait_healthy(port))
        write_pid_file(self.state_dir, "a", decoy.pid)
        return self.config(server_entry("a", port, self.script)), decoy

    def test_refuses_to_kill_when_no_supervisor_is_alive(self):
        raw, decoy = self._decoy_server()
        res = self._check(raw)
        self.assertEqual(res.returncode, 1)
        self.assertIn("supervisor", (res.stdout + res.stderr).lower())
        self.assertIsNone(decoy.poll(), "check killed the server with no supervisor to restart it")

    def test_refuses_when_the_supervisor_crashed(self):
        raw, decoy = self._decoy_server()
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        write_pid_file(self.state_dir, SUPERVISOR_PID_NAME, gone.pid)
        res = self._check(raw)
        self.assertEqual(res.returncode, 1)
        self.assertIn("supervisor", (res.stdout + res.stderr).lower())
        self.assertIsNone(decoy.poll())

    def test_force_kills_without_a_supervisor(self):
        raw, decoy = self._decoy_server()
        res = self._check(raw, "1", "--force")
        self.assertEqual(res.returncode, 1, "nothing restarts it, so no healthy pass")
        self.assertIsNotNone(decoy.poll(), "--force must kill the recorded tree")

    def test_running_supervisor_writes_its_heartbeat(self):
        self.spawn_supervisor(self.config(server_entry("a", free_port(), self.script)))
        beat = self.state_dir / f"{SUPERVISOR_PID_NAME}.pid"
        self.assertTrue(_wait(beat.exists))
        # Not Popen.pid: a venv launcher can sit between it and the interpreter.
        pid = read_pid_file(beat)
        self.assertTrue(pid_alive(pid))
        self.assertNotEqual(pid, os.getpid())

    def test_stopped_supervisor_clears_its_heartbeat(self):
        sup = supervisor.Supervisor(
            load_config(self.config(server_entry("a", free_port(), self.script))),
            poll_interval=0.05)
        sup.stop_event.set()
        sup.run()
        self.assertFalse((self.state_dir / f"{SUPERVISOR_PID_NAME}.pid").exists())

    def test_a_run_that_raises_still_clears_its_heartbeat(self):
        sup = supervisor.Supervisor(
            load_config(self.config(server_entry("a", free_port(), self.script))),
            poll_interval=0.05)
        with mock.patch.object(sup, "_check_running", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                sup.run()
        for tracker in sup.trackers.values():
            if tracker.proc is not None:
                self.cleanup_procs.append(tracker.proc)
        self.assertFalse((self.state_dir / f"{SUPERVISOR_PID_NAME}.pid").exists())

    def test_refuses_without_pid_file(self):
        res = self._check(self.config(server_entry("a", free_port(), self.script)))
        self.assertEqual(res.returncode, 1)
        self.assertIn("pid", (res.stdout + res.stderr).lower())

    def test_refuses_dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        write_pid_file(self.state_dir, "a", proc.pid)
        res = self._check(self.config(server_entry("a", free_port(), self.script)))
        self.assertEqual(res.returncode, 1)
        self.assertIn("not alive", res.stdout + res.stderr)

    def test_healthy_server_with_unchanged_pid_is_not_a_pass(self):
        port = free_port()
        server = subprocess.Popen([sys.executable, str(self.script), str(port)])
        decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.cleanup_procs += [server, decoy]
        self.assertTrue(wait_healthy(port))
        write_pid_file(self.state_dir, "a", decoy.pid)
        self.mark_supervisor_alive()
        res = self._check(self.config(server_entry("a", port, self.script)))
        self.assertEqual(res.returncode, 1, "healthy but never restarted must fail")
        self.assertNotEqual(decoy.poll(), None, "decoy (the pid-file pid) must have been killed")


if __name__ == "__main__":
    unittest.main()
