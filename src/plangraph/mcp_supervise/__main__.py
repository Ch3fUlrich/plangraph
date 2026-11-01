"""CLI entry point for plangraph.mcp_supervise."""

import argparse
import re
import signal
import sys
import threading
import time
from pathlib import Path

from .config import ConfigError, load_config
from .health import probe
from .render import render_json
from .supervisor import kill_process_tree, run_supervisor


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


def _find_child_pid(cfg, name: str) -> int:
    state_dir = Path(cfg.state_dir or "mcp-supervise.state").resolve()
    pid_file = state_dir / f"{name}.pid"
    if pid_file.is_file():
        try:
            val = pid_file.read_text(encoding="utf-8").strip()
            if val and val.isdigit():
                return int(val)
        except Exception:
            pass

    # Fallback to inspecting log file
    log_path = Path(cfg.log_file or "mcp-supervise.log").resolve()
    if log_path.is_file():
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            last_pid = None
            pattern = re.compile(rf"name={re.escape(name)}\s+pid=(\d+)\s+action=(?:start|restart)")
            for line in text.splitlines():
                m = pattern.search(line)
                if m:
                    last_pid = int(m.group(1))
            if last_pid is not None:
                return last_pid
        except Exception:
            pass

    return -1


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    target = None
    for s in cfg.servers:
        if s.name == args.name:
            target = s
            break

    if target is None:
        print(f"Server '{args.name}' not found in configuration", file=sys.stderr)
        return 1

    pid = _find_child_pid(cfg, target.name)
    if pid <= 0:
        print(f"Could not find running child PID for server '{target.name}'", file=sys.stderr)
        return 1

    # Kill that process tree
    kill_process_tree(pid)

    # Poll probe until healthy within 60 s
    url = f"http://{target.host}:{target.port}/mcp"
    start_time = time.monotonic()
    timeout = 60.0

    while time.monotonic() - start_time < timeout:
        if probe(url, timeout=1.0):
            elapsed = time.monotonic() - start_time
            print(f"Server '{target.name}' healthy in {elapsed:.2f}s")
            return 0
        time.sleep(0.5)

    print(f"Server '{target.name}' failed to become healthy within {timeout:.0f}s", file=sys.stderr)
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
