"""CLI entry point for plangraph.mcp_supervise."""

import argparse
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from .config import ConfigError, load_config
from .health import probe
from .portowner import listener_pid
from .procutil import (
    SUPERVISOR_PID_NAME,
    kill_process_tree,
    pid_alive,
    pid_matches,
    read_pid_file,
    read_pid_record,
)
from .render import render_json
from .supervisor import run_supervisor


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    stop_event = threading.Event()

    def _sig_handler(signum, frame):
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
    except Exception:
        pass

    try:
        run_supervisor(cfg, stop_event=stop_event)
    except KeyboardInterrupt:
        stop_event.set()
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    print(render_json(cfg, wrap=args.wrap))
    return 0


def _recorded_pid(name: str, pid_file: Path) -> Tuple[Optional[int], Optional[str]]:
    """(pid, None) when the pid file names the live server process, else (None, why not)."""
    record = read_pid_record(pid_file)
    if record is None:
        return None, f"No pid file for server '{name}' ({pid_file}): is the supervisor running?"
    pid, start = record
    if not pid_alive(pid):
        return None, f"Pid {pid} of server '{name}' is not alive (stale pid file)"
    if not pid_matches(pid, start):
        return None, (f"Pid {pid} of server '{name}' is alive but started at a different "
                      "time than the pid file recorded: the pid was recycled, refusing to kill it")
    return pid, None


def _supervisor_alive(state_dir: Path) -> bool:
    record = read_pid_record(state_dir / f"{SUPERVISOR_PID_NAME}.pid")
    return record is not None and pid_matches(*record)


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    target = next((s for s in cfg.servers if s.name == args.name), None)
    if target is None:
        print(f"Server '{args.name}' not found in configuration", file=sys.stderr)
        return 1

    state_dir = Path(cfg.state_dir or "mcp-supervise.state").resolve()
    pid_file = state_dir / f"{target.name}.pid"
    old_pid, problem = _recorded_pid(target.name, pid_file)
    if problem:
        # An adopted server whose owner was unresolvable has no usable pid file: the
        # process listening on its port is the server.
        old_pid = listener_pid(target.host, target.port)
        if old_pid is None:
            print(problem, file=sys.stderr)
            return 1

    if not args.force and not _supervisor_alive(state_dir):
        print(f"No live supervisor found in {state_dir}: killing '{target.name}' would leave it "
              "dead with nothing to restart it. Start the supervisor, or pass --force.",
              file=sys.stderr)
        return 1

    if not kill_process_tree(old_pid, log=lambda m: print(m, file=sys.stderr)):
        print(f"Could not kill pid {old_pid} of server '{target.name}'", file=sys.stderr)
        return 1

    # Healthy alone is not enough: the old process may still be answering, or an
    # orphan may be. Require the pid file to name a different, live process.
    url = f"http://{target.host}:{target.port}/mcp"
    start_time = time.monotonic()
    while time.monotonic() - start_time < args.timeout:
        new_pid = read_pid_file(pid_file)
        if new_pid is not None and new_pid != old_pid and pid_alive(new_pid) \
                and probe(url, timeout=1.0):
            elapsed = time.monotonic() - start_time
            print(f"Server '{target.name}' healthy in {elapsed:.2f}s "
                  f"(pid {old_pid} -> {new_pid})")
            return 0
        time.sleep(0.5)

    print(f"Server '{target.name}' did not become healthy with a new pid within "
          f"{args.timeout:g}s", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m plangraph.mcp_supervise",
        description="Supervised HTTP MCP server runner",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = subparsers.add_parser("run", help="Run the supervisor daemon")
    p_run.add_argument("--config", "-c", required=True, help="Path to JSON config file")

    # render
    p_render = subparsers.add_parser("render", help="Render Claude Code mcpServers configuration")
    p_render.add_argument("--config", "-c", required=True, help="Path to JSON config file")
    p_render.add_argument("--wrap", action="store_true", help="Wrap output in mcpServers key")

    # check
    p_check = subparsers.add_parser("check", help="Kill test: kill child process and verify healthy restart")
    p_check.add_argument("--config", "-c", required=True, help="Path to JSON config file")
    p_check.add_argument("--name", "-n", required=True, help="Server name to test")
    p_check.add_argument("--timeout", type=float, default=60.0, help="Seconds to wait for recovery (default 60)")

    p_check.add_argument("--force", action="store_true",
                         help="Kill even when no live supervisor is found (nothing will restart it)")

    args = parser.parse_args(argv)

    if args.command == "run":
        return _cmd_run(args)
    elif args.command == "render":
        return _cmd_render(args)
    elif args.command == "check":
        return _cmd_check(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
