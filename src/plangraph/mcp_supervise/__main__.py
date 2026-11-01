"""CLI entry point for plangraph.mcp_supervise."""

import argparse
import signal
import sys
import threading
import time
from pathlib import Path

from .config import ConfigError, load_config
from .health import probe
from .procutil import kill_process_tree, pid_alive, read_pid_file
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
    old_pid = read_pid_file(pid_file)
    if old_pid is None:
        print(f"No pid file for server '{target.name}' ({pid_file}): is the supervisor running?",
              file=sys.stderr)
        return 1
    if not pid_alive(old_pid):
        print(f"Pid {old_pid} of server '{target.name}' is not alive (stale pid file)",
              file=sys.stderr)
        return 1

    kill_process_tree(old_pid)

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
