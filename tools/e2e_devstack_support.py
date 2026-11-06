"""Preflight validation and owned-process support for the devstack E2E driver."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import stat
import subprocess
import time
import urllib.parse
from pathlib import Path

import rtp1_local_util as u

REPO = Path(__file__).resolve().parents[1]
# Live/default Temporal, UI, Postgres and keeper ports must never be touched.
REFUSED_PORTS = frozenset({7233, 7243, 8233, 55432, 8787})


class Fail(Exception):
    """Any failed step exits 1 with this as the reason."""


def step(msg: str) -> None:
    print(f"[step] {msg}", flush=True)


def assert_verbs(seen: list[str], required: tuple[str, ...], forbidden: tuple[str, ...] = ()) -> None:
    missing = [v for v in required if v not in seen]
    if missing:
        raise Fail(f"keeper did not serve the required verb(s): {', '.join(missing)}")
    present = [v for v in forbidden if v in seen]
    if present:
        raise Fail(f"keeper served verb(s) that must not occur on this leg: {', '.join(present)}")


def _refuse_port(port: int | str, what: str) -> None:
    if int(port) in REFUSED_PORTS:
        raise Fail(f"{what} must not use the live/default port {port}")


def _hostport(addr: str, what: str) -> tuple[str, str]:
    host, sep, port = addr.rpartition(":")
    if not sep or not port.isdigit():
        raise Fail(f"{what} must be HOST:PORT")
    if host != "127.0.0.1":
        raise Fail(f"{what} must be loopback")
    _refuse_port(port, what)
    return host, port


def _check_url(url: str, what: str) -> str:
    parsed = urllib.parse.urlparse(url if "//" in url else f"http://{url}")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise Fail(f"{what} is not a URL")
    if parsed.hostname != "127.0.0.1":
        raise Fail(f"{what} must be loopback")
    if parsed.username is not None or parsed.password is not None:
        raise Fail(f"{what} must not contain credentials")
    if parsed.port is None:
        raise Fail(f"{what} needs an explicit port")
    _refuse_port(parsed.port, what)
    return f"{parsed.scheme}://{parsed.netloc}"


def _run(cmd: list[str], what: str, *, env=None, timeout=120, pass_fds=()) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=REPO, env=env, stdin=subprocess.DEVNULL, pass_fds=pass_fds)
    except OSError as exc:
        raise Fail(f"{what}: could not execute command") from exc
    if done.returncode != 0:
        raise Fail(f"{what} exited {done.returncode} (output withheld)")
    return done


def wait_until(predicate, what: str, timeout: float, step_s: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step_s)
    raise Fail(f"timeout waiting for {what}")


def write_failing_opencode(bin_dir: Path) -> None:
    """An `opencode` that always dies: exit 1 with unknown stderr, so the worker cannot map it
    to a known exit class. The worker classifies it as `worker_crash`, retries it
    `retries_per_tier` times (3) and parks the card with an alarm (budget/mod.rs policy order).
    Temp name + rename, like `u.write_fake_opencode`, so the worker never execs a half-written file.
    """
    bin_dir.mkdir(parents=True)
    tmp = bin_dir / "opencode.tmp"
    tmp.write_text("#!/bin/sh\necho segfault >&2\nexit 1\n", encoding="utf-8")
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR)
    os.replace(tmp, bin_dir / "opencode")


def write_stub_opencode(bin_dir: Path) -> None:
    """Emit an opencode text event on resume, not unrecognized plain stdout."""
    u.write_fake_opencode(bin_dir)
    path = bin_dir / "opencode"
    path.write_text(path.read_text().replace(
        'echo "wrote out.txt"',
        "echo '{\"type\":\"text\",\"part\":{\"type\":\"text\",\"text\":\"wrote out.txt\"}}'"))


def stop_worker(worker) -> None:
    """Only the Popen child we own; never discover workers by name or kill a service."""
    try:
        pgid = getattr(worker, "owned_pgid", None)
        def send(sig):
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                elif worker.proc.poll() is None:
                    worker.proc.send_signal(sig)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            send(signal.SIGTERM)
            if pgid is not None:
                # WNOWAIT pins the leader's pid until the last group signal (no pgid reuse).
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if os.waitid(os.P_PID, worker.proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                        break
                    time.sleep(0.05)
            elif worker.proc.poll() is None:
                try:
                    worker.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            previous, pending = {}, []
            def defer(signum, frame):
                pending.append((signum, frame))
            try:
                # Handlers cover delivery to any thread; pthread_sigmask does not.
                for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
                    previous[sig] = signal.signal(sig, defer)
                send(signal.SIGKILL)  # also kill descendants after the parent has exited
                worker.proc.wait(timeout=10)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                if pending:
                    sig, frame = pending[0]
                    handler = previous[sig]
                    if callable(handler):
                        handler(sig, frame)
                    elif handler == signal.SIG_DFL:
                        signal.raise_signal(sig)
    finally:
        worker.out.close()


def interrupted(signum, _frame) -> None:
    # Do not let a second termination interrupt the kill-and-reap finally blocks.
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
        signal.signal(sig, signal.SIG_IGN)
    raise Fail("run time box expired" if signum == signal.SIGALRM else "run interrupted")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=(
        "RT-P1 end-to-end run against an already-running devstack (no pytest, standalone)."))
    p.add_argument("--temporal", default=os.environ.get("TEMPORAL_ADDR", ""),
                   help="dev server HOST:PORT (env TEMPORAL_ADDR), required")
    p.add_argument("--temporal-http", default=os.environ.get("PLANGRAPH_TEMPORAL_HTTP", ""),
                   help="Temporal HTTP API the keeper signals through (env PLANGRAPH_TEMPORAL_HTTP)")
    p.add_argument("--keeper", default=os.environ.get("KEEPER_URL", ""),
                   help="dev keeper URL (env KEEPER_URL), required")
    p.add_argument("--state", default=os.environ.get("DEVSTACK_STATE", str(REPO / ".devstack")),
                   help="devstack state dir holding keeper.json (env DEVSTACK_STATE)")
    p.add_argument("--worker-bin", default=os.environ.get("PG_WORKER_BIN", ""),
                   help="built pg-worker (env PG_WORKER_BIN)")
    p.add_argument("--temporal-cli", default=os.environ.get("TEMPORAL_CLI") or shutil.which("temporal"),
                   help="temporal CLI binary (env TEMPORAL_CLI)")
    p.add_argument("--leg", choices=("stub", "stub-fail", "opencode"), default="stub")
    p.add_argument("--model", default="", help="opencode model id for --leg opencode")
    p.add_argument("--keep", action="store_true", help="keep the scratch dir")
    p.add_argument("--box-seconds", type=int, default=1800,
                   help="whole-run deadline in seconds (default: 1800)")
    return p.parse_args(argv)


def validate_args(args) -> tuple[str, str]:
    """No-filesystem, no-network argument checks; returns (keeper_url, temporal_http_url_or_empty)."""
    if not args.temporal:
        raise Fail("--temporal HOST:PORT (or env TEMPORAL_ADDR) is required: point it at the "
                   "devstack's own Temporal port, never the default 7233")
    if not args.keeper:
        raise Fail("--keeper URL (or env KEEPER_URL) is required: the devstack keeper, never "
                   "the live 8787")
    _hostport(args.temporal, "--temporal")
    keeper = _check_url(args.keeper, "--keeper")
    http = _check_url(args.temporal_http, "--temporal-http") if args.temporal_http else ""
    if args.leg == "opencode" and not args.model:
        raise Fail("--leg opencode needs --model <opencode model id>")
    if not http:
        raise Fail("PLANGRAPH_TEMPORAL_HTTP is required (or --temporal-http); "
                   "the keeper must also have Temporal HTTP signalling enabled")
    if args.box_seconds <= 0:
        raise Fail("--box-seconds must be positive")
    return keeper, http


def check_keeper_config(cfg: Path) -> None:
    """The keeper config must not point at the live Postgres or keeper bind either."""
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Fail("keeper config unreadable") from exc
    try:
        dsn = urllib.parse.urlparse(str(data.get("dsn", "")))
        if dsn.scheme not in ("postgresql", "postgres") or dsn.query or dsn.fragment:
            raise Fail("keeper.json dsn must be a Postgres URL without overrides")
        if dsn.hostname != "127.0.0.1":
            raise Fail("keeper.json dsn must be loopback")
        if dsn.port is None:
            raise Fail("keeper.json dsn needs an explicit port")
        _refuse_port(dsn.port, "keeper.json dsn")
    except ValueError as exc:
        raise Fail("keeper.json dsn is invalid") from exc
    _host, sep, bind_port = str(data.get("bind", "")).rpartition(":")
    if sep and bind_port.isdigit():
        _refuse_port(bind_port, "keeper.json bind")


def check_devstack(args, keeper: str, http: str) -> None:
    """Fail closed unless endpoints match the selected state's running server."""
    state = Path(args.state).resolve()
    try:
        data = json.loads((state / "keeper.json").read_text())
        if keeper != "http://" + data["bind"]:
            raise Fail("keeper_state_mismatch")
        pid = int((state / "temporal.pid").read_text())
        if pid <= 0:
            raise ValueError
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
        expected = {"--ip": "127.0.0.1", "--port": args.temporal.rsplit(":", 1)[1],
                    "--http-port": str(urllib.parse.urlparse(http).port),
                    "--db-filename": str(state / "temporal.db")}
        if not any(cmd[i:i + 2] == ["server", "start-dev"] for i in range(len(cmd))):
            raise Fail("temporal_state_mismatch")
        for flag, value in expected.items():
            if cmd.count(flag) != 1 or cmd[cmd.index(flag) + 1] != value:
                raise Fail("temporal_state_mismatch")
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise Fail("devstack_state_unverifiable") from exc
