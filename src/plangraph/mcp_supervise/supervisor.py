"""Process supervisor for MCP servers."""

import datetime
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import ServerConfig, SuperviseConfig
from .health import probe
from .portowner import listener_pid
from .procutil import (
    SUPERVISOR_PID_NAME,
    kill_process_tree,
    pid_matches,
    read_pid_record,
    write_pid_file,
)

MCP_PROXY_PINNED_VERSION = "0.13.0"
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 30.0
HEALTHY_RESET_SECONDS = 300.0  # process uptime, not probe success: running children are not probed
PROBE_TIMEOUT = 1.0
ADOPT_RECHECK_SECONDS = 2.0
# A child that dies this soon after spawn, while something else holds its port and
# fails the probe, counts as a strike; after STALE_LISTENER_STRIKES the holder is killed.
STALE_STRIKE_WINDOW = 30.0
STALE_LISTENER_STRIKES = 3


def calculate_backoff(current_backoff: float) -> float:
    """Double the backoff interval, capped at MAX_BACKOFF (30.0 s)."""
    return min(MAX_BACKOFF, current_backoff * 2.0)


def _proxy_version(proxy_bin: str) -> Optional[str]:
    """Version reported by `mcp-proxy --version` ("mcp-proxy 0.13.0" -> "0.13.0")."""
    try:
        out = subprocess.run(
            [proxy_bin, "--version"], capture_output=True, text=True, timeout=15, check=False
        ).stdout.split()
    except Exception:
        return None
    return out[-1] if out else None


def resolve_proxy_prefix(
    which: Callable[[str], Optional[str]] = shutil.which,
    proxy_version: Callable[[str], Optional[str]] = _proxy_version,
) -> List[str]:
    """The proxy launcher prefix: pinned `uvx mcp-proxy==<pin>`, unless a PATH
    `mcp-proxy` reports exactly the pin. Spawns `--version`, so callers cache it."""
    proxy_bin = which("mcp-proxy")
    if proxy_bin and proxy_version(proxy_bin) == MCP_PROXY_PINNED_VERSION:
        return [proxy_bin]
    return [which("uvx") or "uvx", f"mcp-proxy=={MCP_PROXY_PINNED_VERSION}"]


def build_child_command(
    server: ServerConfig,
    which: Callable[[str], Optional[str]] = shutil.which,
    proxy_version: Callable[[str], Optional[str]] = _proxy_version,
    prefix: Optional[List[str]] = None,
) -> List[str]:
    """Construct the command line for a supervised server child process.

    The pinned `uvx mcp-proxy==<pin>` form is the default (see resolve_proxy_prefix);
    pass `prefix` to reuse an already resolved launcher.
    """
    if not server.proxy:
        return list(server.cmd)

    if prefix is None:
        prefix = resolve_proxy_prefix(which, proxy_version)

    # --pass-environment: without it mcp-proxy hands the server only a minimal
    # environment, dropping PYTHONUTF8 and the configured env. Exercised against the
    # real pinned proxy by tests/test_mcp_supervise_proxy.py.
    flags = [
        "--host",
        server.host,
        "--port",
        str(server.port),
        "--stateless",
        "--pass-environment",
        "--",
    ]
    return list(prefix) + flags + list(server.cmd)


class ProcessTracker:
    def __init__(self, server: ServerConfig) -> None:
        self.server = server
        self.proc: Optional[subprocess.Popen] = None
        self.adopted: bool = False
        self.current_backoff: float = INITIAL_BACKOFF
        self.last_delay: float = 0.0
        self.next_restart_time: float = 0.0
        self.next_probe_time: float = 0.0
        self.started_at: float = 0.0
        self.total_restarts: int = 0
        self.stale_strikes: int = 0


class Supervisor:
    def __init__(
        self,
        config: SuperviseConfig,
        stop_event: Optional[threading.Event] = None,
        poll_interval: float = 0.1,
        which: Callable[[str], Optional[str]] = shutil.which,
        proxy_version: Callable[[str], Optional[str]] = _proxy_version,
    ) -> None:
        self.config = config
        self._which = which
        self._proxy_version = proxy_version
        self._proxy_prefix: Optional[List[str]] = None
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
            write_pid_file(self.state_dir, name, pid)
        except Exception:
            pass

    def _schedule_restart(self, tracker: ProcessTracker, now: float) -> None:
        tracker.last_delay = tracker.current_backoff
        tracker.next_restart_time = now + tracker.last_delay
        tracker.current_backoff = calculate_backoff(tracker.last_delay)

    def _command(self, server: ServerConfig) -> List[str]:
        if server.proxy and self._proxy_prefix is None:
            self._proxy_prefix = resolve_proxy_prefix(self._which, self._proxy_version)
        return build_child_command(server, prefix=self._proxy_prefix)

    def _kill(self, name: str, pid: int) -> bool:
        def note(msg: str) -> None:
            self._log(name, pid, "kill_failed", None, msg.replace(" ", "_"))

        return kill_process_tree(pid, log=note)

    def _adopt(self, tracker: ProcessTracker, reason: str) -> None:
        """Something already answers on the port (e.g. a child that outlived a
        supervisor crash): leave it running instead of crash-looping on the bind,
        and make sure the pid file names it so `check` can find it."""
        server = tracker.server
        name = server.name
        record = read_pid_record(self.state_dir / f"{name}.pid")
        if record is not None and pid_matches(*record):
            pid: Optional[int] = record[0]
        else:
            pid = listener_pid(server.host, server.port)
            if pid is None:
                # Writing None would delete the pid file; `check` falls back to the
                # port listener instead.
                self._log(name, None, "adopt_pid_unknown", None, "listener_owner_unresolved")
            else:
                self._write_pid_file(name, pid)
        tracker.adopted = True
        tracker.proc = None
        tracker.next_probe_time = time.time() + ADOPT_RECHECK_SECONDS
        self._log(name, pid, "adopted_existing", None, reason)

    def _url(self, server: ServerConfig) -> str:
        return f"http://{server.host}:{server.port}/mcp"

    def _start_child(self, tracker: ProcessTracker, reason: str) -> None:
        server = tracker.server
        if probe(self._url(server), timeout=PROBE_TIMEOUT):
            self._adopt(tracker, reason)
            return

        cwd = server.project_dir
        if cwd and not Path(cwd).is_dir():
            # Never fall back to the supervisor's cwd: a project server started outside
            # its project would activate the wrong workspace. Retry with backoff in case
            # the directory appears (drive not mounted yet).
            self._log(server.name, None, "error", None, "project_dir_missing")
            self._schedule_restart(tracker, time.time())
            return

        env = dict(os.environ)
        env.update(server.env)
        env["PYTHONUTF8"] = "1"

        try:
            cmd = self._command(server)
            # No shell. Own session/process group so a tree kill of this child
            # cannot reach the supervisor or its sibling servers (ignored on Windows).
            proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=cwd or None,
                shell=False,
                start_new_session=True,
            )
            tracker.proc = proc
            tracker.started_at = time.time()
            self._write_pid_file(server.name, proc.pid)
            action = "restart" if tracker.total_restarts > 0 else "start"
            self._log(server.name, proc.pid, action, None, reason)
        except Exception as e:
            # Class name only: the message may echo argv, which can hold tokens.
            self._log(server.name, None, "error", None, f"spawn_failed: {type(e).__name__}")
            tracker.proc = None
            self._schedule_restart(tracker, time.time())

    def _count_stale_listener(self, tracker: ProcessTracker, died_after: float) -> None:
        """A child that died young while another process still holds (and does not
        serve) its port will keep dying on the bind: after a few strikes, kill the holder."""
        server = tracker.server
        holder = None if died_after >= STALE_STRIKE_WINDOW else listener_pid(
            server.host, server.port)
        if holder is None or holder == os.getpid() or probe(
                self._url(server), timeout=PROBE_TIMEOUT):
            tracker.stale_strikes = 0
            return
        tracker.stale_strikes += 1
        if tracker.stale_strikes < STALE_LISTENER_STRIKES:
            return
        tracker.stale_strikes = 0
        if self._kill(server.name, holder):
            self._log(server.name, holder, "stale_listener_killed", None,
                      "port_held_without_serving")

    def _check_adopted(self, tracker: ProcessTracker, now: float) -> None:
        if now < tracker.next_probe_time:
            return
        if probe(self._url(tracker.server), timeout=PROBE_TIMEOUT):
            tracker.next_probe_time = now + ADOPT_RECHECK_SECONDS
            return
        tracker.adopted = False
        self._log(tracker.server.name, None, "adopted_gone", None, "probe_failed")
        self._start_child(tracker, reason="adopted_stopped")

    def _check_running(self, tracker: ProcessTracker, proc: subprocess.Popen, now: float) -> None:
        rc = proc.poll()
        if rc is None:
            if now - tracker.started_at >= HEALTHY_RESET_SECONDS:
                if tracker.current_backoff != INITIAL_BACKOFF:
                    tracker.current_backoff = INITIAL_BACKOFF
                    self._log(
                        tracker.server.name, proc.pid, "healthy", None,
                        "reset_backoff_after_300s",
                    )
            return
        self._log(tracker.server.name, proc.pid, "exit", rc, f"process_exited_rc_{rc}")
        self._write_pid_file(tracker.server.name, None)
        tracker.proc = None
        self._count_stale_listener(tracker, now - tracker.started_at)
        tracker.total_restarts += 1
        self._schedule_restart(tracker, now)

    def run(self) -> None:
        """Main supervisor loop."""
        self._write_pid_file(SUPERVISOR_PID_NAME, os.getpid())
        for tracker in self.trackers.values():
            self._start_child(tracker, reason="initial_start")

        while not self.stop_event.is_set():
            now = time.time()
            for tracker in self.trackers.values():
                if tracker.adopted:
                    self._check_adopted(tracker, now)
                elif tracker.proc is not None:
                    self._check_running(tracker, tracker.proc, now)
                elif now >= tracker.next_restart_time:
                    self._start_child(
                        tracker, reason=f"backoff_restart_{tracker.last_delay:g}s"
                    )

            self.stop_event.wait(timeout=self.poll_interval)

        # Cleanup on stop: only children this supervisor started; adopted ones stay.
        for tracker in self.trackers.values():
            if tracker.proc is not None:
                pid = tracker.proc.pid
                self._kill(tracker.server.name, pid)
                try:
                    tracker.proc.wait(timeout=2.0)
                except Exception:
                    pass
                self._log(tracker.server.name, pid, "exit", tracker.proc.returncode, "supervisor_shutdown")
                self._write_pid_file(tracker.server.name, None)
        self._write_pid_file(SUPERVISOR_PID_NAME, None)


def run_supervisor(
    config: SuperviseConfig,
    stop_event: Optional[threading.Event] = None,
    poll_interval: float = 0.1,
) -> None:
    """Run the supervisor with the given configuration."""
    sup = Supervisor(config, stop_event=stop_event, poll_interval=poll_interval)
    sup.run()


run = run_supervisor
