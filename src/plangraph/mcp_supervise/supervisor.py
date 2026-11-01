"""Process supervisor for MCP servers."""

import datetime
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import ServerConfig, SuperviseConfig

MCP_PROXY_PINNED_VERSION = "0.13.0"
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 30.0
HEALTHY_RESET_SECONDS = 300.0


def calculate_backoff(current_backoff: float) -> float:
    """Double the backoff interval, capped at MAX_BACKOFF (30.0 s)."""
    return min(MAX_BACKOFF, current_backoff * 2.0)


def kill_process_tree(pid: int) -> None:
    """Terminate a process and all its child processes."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass
    else:
        try:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def build_child_command(server: ServerConfig) -> List[str]:
    """Construct the command line for a supervised server child process."""
    if not server.proxy:
        return list(server.cmd)

    proxy_bin = shutil.which("mcp-proxy")
    if proxy_bin:
        prefix = [proxy_bin]
    else:
        uvx_bin = shutil.which("uvx") or "uvx"
        prefix = [uvx_bin, f"mcp-proxy=={MCP_PROXY_PINNED_VERSION}"]

    flags = [
        "--host",
        server.host,
        "--port",
        str(server.port),
        "--stateless",
        "--pass-environment",
        "--",
    ]
    return prefix + flags + list(server.cmd)


class ProcessTracker:
    def __init__(self, server: ServerConfig) -> None:
        self.server = server
        self.proc: Optional[subprocess.Popen] = None
        self.current_backoff: float = INITIAL_BACKOFF
        self.next_restart_time: float = 0.0
        self.started_at: float = 0.0
        self.total_restarts: int = 0


class Supervisor:
    def __init__(
        self,
        config: SuperviseConfig,
        stop_event: Optional[threading.Event] = None,
        poll_interval: float = 0.1,
    ) -> None:
        self.config = config
        self.stop_event = stop_event or threading.Event()
        self.poll_interval = poll_interval
        self.log_path = Path(config.log_file or "mcp-supervise.log").resolve()
        self.state_dir = Path(config.state_dir or "mcp-supervise.state").resolve()
        self.trackers: Dict[str, ProcessTracker] = {
            s.name: ProcessTracker(s) for s in config.servers
        }

    def _log(
        self,
        name: str,
        pid: Optional[int],
        action: str,
        rc: Optional[int],
        reason: str,
    ) -> None:
        """Write one structured log line. Never logs environment values."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        line = (
            f"{now} name={name} pid={pid if pid is not None else 'None'} "
            f"action={action} rc={rc if rc is not None else 'None'} reason={reason}\n"
        )
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _write_pid_file(self, name: str, pid: Optional[int]) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            pid_file = self.state_dir / f"{name}.pid"
            if pid is not None:
                pid_file.write_text(str(pid), encoding="utf-8")
            elif pid_file.exists():
                pid_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _start_child(self, tracker: ProcessTracker, reason: str) -> None:
        server = tracker.server
        cmd = build_child_command(server)
        env = dict(os.environ)
        env.update(server.env)
        # PYTHONUTF8=1 always set in child env
        env["PYTHONUTF8"] = "1"

        cwd = server.project_dir if server.project_dir else None
        if cwd and not Path(cwd).is_dir():
            # If specified project directory doesn't exist yet, run without cwd or error
            cwd = None

        try:
            # Process-tree start must not use a shell
            proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=cwd,
                shell=False,
            )
            tracker.proc = proc
            tracker.started_at = time.time()
            self._write_pid_file(server.name, proc.pid)
            action = "restart" if tracker.total_restarts > 0 else "start"
            self._log(server.name, proc.pid, action, None, reason)
        except Exception as e:
            self._log(server.name, None, "error", None, f"spawn_failed: {e}")
            tracker.proc = None
            tracker.next_restart_time = time.time() + tracker.current_backoff
            tracker.current_backoff = calculate_backoff(tracker.current_backoff)

    def run(self) -> None:
        """Main supervisor loop."""
        for tracker in self.trackers.values():
            self._start_child(tracker, reason="initial_start")

        while not self.stop_event.is_set():
            now = time.time()
            for tracker in self.trackers.values():
                proc = tracker.proc
                if proc is not None:
                    rc = proc.poll()
                    if rc is None:
                        # Process is still running. Check if healthy >= 300 s
                        if now - tracker.started_at >= HEALTHY_RESET_SECONDS:
                            if tracker.current_backoff != INITIAL_BACKOFF:
                                tracker.current_backoff = INITIAL_BACKOFF
                                self._log(
                                    tracker.server.name,
                                    proc.pid,
                                    "healthy",
                                    None,
                                    "reset_backoff_after_300s",
                                )
                    else:
                        # Process exited
                        self._log(
                            tracker.server.name,
                            proc.pid,
                            "exit",
                            rc,
                            f"process_exited_rc_{rc}",
                        )
                        self._write_pid_file(tracker.server.name, None)
                        tracker.proc = None
                        tracker.total_restarts += 1
                        delay = tracker.current_backoff
                        tracker.next_restart_time = now + delay
                        tracker.current_backoff = calculate_backoff(delay)
                else:
                    # Waiting for restart time
                    if now >= tracker.next_restart_time:
                        reason = f"backoff_restart_{tracker.current_backoff:.0f}s"
                        self._start_child(tracker, reason=reason)

            self.stop_event.wait(timeout=self.poll_interval)

        # Cleanup on stop
        for tracker in self.trackers.values():
            if tracker.proc is not None:
                pid = tracker.proc.pid
                kill_process_tree(pid)
                try:
                    tracker.proc.wait(timeout=2.0)
                except Exception:
                    pass
                self._log(tracker.server.name, pid, "exit", tracker.proc.returncode, "supervisor_shutdown")
                self._write_pid_file(tracker.server.name, None)


def run_supervisor(
    config: SuperviseConfig,
    stop_event: Optional[threading.Event] = None,
    poll_interval: float = 0.1,
) -> None:
    """Run the supervisor with the given configuration."""
    sup = Supervisor(config, stop_event=stop_event, poll_interval=poll_interval)
    sup.run()


run = run_supervisor
