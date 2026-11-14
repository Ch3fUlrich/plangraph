#!/usr/bin/env python3
"""Switch the live keeper to a new tree, smoke it, and roll back on failure.

    keeper_deploy.py --tag <tag> --sha <sha> --tree <worktree> --probe-key <key> [--apply]

Replaces managed systemd overrides with ``zzzz-plangraph-live-sync.conf`` (WorkingDirectory
and PYTHONPATH of the new tree), runs daemon-reload + restart, verifies the effective tree, then a live smoke
within ``--smoke-timeout`` seconds (default 60):

* ``/v1/health`` answers;
* claim + release of the dedicated probe item (the claim guard reads the
  origin decisions fresh, so "bad_state: cannot read decisions file" shows here);
* ``close_merged`` returns the expected role refusal (claimants lack router scope),
  or an explicit dry-run answer for a separately authorized session.

On any failure the previous drop-in set is restored byte for byte, then
daemon-reload, restart and a health check run, and the exit code is 1 with the
reason on stderr.  Environment as for ``pg``: PG_URL, PG_TOKEN_FILE.
"""
import argparse
from datetime import datetime, timezone
import os
import shutil
import tempfile
from contextlib import contextmanager
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from plangraph.keeper.cli_verbs import RefusedCli, Session, Unreachable  # noqa: E402

UNIT = "plangraph-keeper.service"
DROPIN_DIR = Path.home() / ".config/systemd/user" / (UNIT + ".d")
MANAGED_DROPIN = "zzzz-plangraph-live-sync.conf"
SHOW = ("show", UNIT, "--property=WorkingDirectory", "--property=Environment")


def systemctl(*args):
    """Run ``systemctl --user``; raise RuntimeError when it fails."""
    done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise RuntimeError(f"systemctl {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def snapshot_dropins(dropin_dir):
    """``{name: bytes}`` of every drop-in file in the directory."""
    if not dropin_dir.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(dropin_dir.glob("*.conf"))}


def restore_dropins(dropin_dir, saved):
    """Make the directory hold exactly the saved drop-in set."""
    dropin_dir.mkdir(parents=True, exist_ok=True)
    for path in dropin_dir.glob("*.conf"):
        if path.name not in saved:
            path.unlink()
    for name, data in saved.items():
        (dropin_dir / name).write_bytes(data)


def backup_dropins(dropin_dir):
    """Persist the previous set before modifying any active drop-in."""
    dropin_dir.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S-')
    backup = Path(tempfile.mkdtemp(prefix=dropin_dir.name + '.backup-' + stamp,
                                   dir=dropin_dir.parent))
    for path in sorted(dropin_dir.glob('*.conf')):
        target = backup / path.name
        shutil.copy2(path, target)
        with target.open('rb') as handle:
            os.fsync(handle.fileno())
    for directory in (backup, backup.parent):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    print(f'Previous drop-ins backup: {backup}', flush=True)
    return backup


@contextmanager
def deployment_signals():
    def interrupted(signum, _frame):
        raise InterruptedError(f'deployment interrupted by signal {signum}')
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def dropin_text(tag, sha, tree):
    if any(not value or any(c.isspace() or not c.isprintable() or c == '%' for c in value)
           for value in (tag, sha, tree)):
        raise ValueError('tag, sha and tree must contain no whitespace, control characters or percent signs')
    return (f"# deploy {tag} {sha} (tools/keeper_deploy.py; rolled back when the smoke fails)\n"
            f"[Service]\nWorkingDirectory={tree}\nEnvironment=PYTHONPATH={tree}/src\n")


def wait_health(session, deadline, sleep=time.sleep, clock=time.monotonic):
    """True once ``/v1/health`` answers before the deadline (monotonic seconds)."""
    while True:
        try:
            session.call("GET", "health")
            return True
        except (Unreachable, RefusedCli):
            if clock() >= deadline:
                return False
            sleep(1)


@contextmanager
def smoke_deadline(timeout):
    """Bound even a blocked request on this systemd deployment CLI's main thread."""
    # SIGALRM handlers can only be installed on the main thread; this is a CLI helper.
    def expired(*_):
        raise TimeoutError('smoke deadline exceeded')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def smoke(session, probe_key, timeout, sleep=time.sleep, clock=time.monotonic):
    with smoke_deadline(timeout):
        return _smoke(session, probe_key, timeout, sleep, clock)


def _smoke(session, probe_key, timeout, sleep=time.sleep, clock=time.monotonic):
    """``None`` when the live keeper passes, else the reason string."""
    deadline = clock() + timeout
    if not wait_health(session, deadline, sleep, clock):
        return f"health did not answer within {timeout}s"
    epoch = None
    released = False
    try:
        claim = session.call("POST", "claim", body={"key": probe_key, "lease_s": 120})
        epoch = claim.get("epoch", (claim.get("item") or {}).get("epoch"))
        if epoch is None:
            return f"claim of {probe_key} returned no epoch"
        session.call("POST", "release", body={"key": probe_key, "epoch": epoch})
        released = True
    except RefusedCli as exc:
        return f"claim/release of {probe_key} refused: {exc.code}: {exc.msg}"
    except Unreachable as exc:
        return f"claim/release of {probe_key} failed: {exc}"
    finally:
        if epoch is not None and not released:
            try:
                with smoke_deadline(min(timeout, 5)):
                    session.call("POST", "release", body={"key": probe_key, "epoch": epoch})
            except BaseException:
                pass  # Best effort; the lease still bounds an unreachable keeper.
    try:
        answer = session.call("POST", "close_merged", body={"dry_run": True})
        if not isinstance(answer, dict) or answer.get('dry_run') is not True or answer.get('route') != 'close_merged':
            return 'close_merged did not return a dry-run answer'
    except RefusedCli as exc:
        if exc.code == "forbidden":
            return None  # route exists; the successful claimant has no router role
        if exc.code == "not_found":
            return "close_merged route missing"
        return f"close_merged dry-run refused: {exc.code}: {exc.msg}"
    except Unreachable as exc:
        return f"close_merged route failed: {exc}"
    return None


def verify_tree(ctl, tree):
    """Refuse to smoke an older checkout selected by another systemd override."""
    properties = dict(line.split("=", 1) for line in ctl(*SHOW).splitlines() if "=" in line)
    environment = dict(entry.split("=", 1) for entry in
                       shlex.split(properties.get("Environment", "")) if "=" in entry)
    working = properties.get("WorkingDirectory")
    if not working or Path(working).resolve() != Path(tree).resolve():
        raise RuntimeError("effective WorkingDirectory does not select the requested tree")
    if environment.get("PYTHONPATH") != str(Path(tree) / "src"):
        raise RuntimeError("effective PYTHONPATH does not select the requested tree")


def deploy(tag, sha, tree, probe_key, session, dropin_dir=DROPIN_DIR, ctl=systemctl,
           timeout=60, sleep=time.sleep, clock=time.monotonic):
    """Return ``(0, "ok")`` or ``(1, reason)``; the previous drop-in set survives a failed smoke."""
    # Validate before backup or any mutation, including callers outside the CLI.
    text = dropin_text(tag, sha, tree)
    with deployment_signals():
        return _deploy(text, tree, probe_key, session, dropin_dir, ctl, timeout, sleep, clock)


def _deploy(text, tree, probe_key, session, dropin_dir, ctl, timeout, sleep, clock):
    reason, touched, success = None, False, False
    note = None
    try:
        backup = backup_dropins(dropin_dir)
        dropin_dir.mkdir(parents=True, exist_ok=True)
        touched = True  # Set before the first unlink/write, which may fail partway.
        for path in dropin_dir.glob("live-sync-*.conf"):
            path.unlink()
        (dropin_dir / MANAGED_DROPIN).write_text(text)
        ctl("daemon-reload")
        ctl("restart", UNIT)
        verify_tree(ctl, tree)
        reason = smoke(session, probe_key, timeout, sleep, clock)
        success = reason is None
    except BaseException as exc:
        reason = str(exc) or type(exc).__name__
    finally:
        if touched and not success:
            # A second operator signal must not interrupt recovery.
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, signal.SIG_IGN)
            note = f"smoke failed: {reason}"
            try:
                restore_dropins(dropin_dir, snapshot_dropins(backup))
                note += "; previous drop-ins restored"
            except BaseException as exc:
                note += f"; ROLLBACK FAILED: {exc}"
            try:
                ctl("daemon-reload")
                ctl("restart", UNIT)
                with smoke_deadline(timeout):
                    if not wait_health(session, clock() + timeout, sleep, clock):
                        note += "; ROLLBACK UNHEALTHY: health did not answer after restore"
            except BaseException as exc:
                note += f"; ROLLBACK FAILED: {exc}"
    return (0, "ok") if success else (1, note or f"deploy failed before write: {reason}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--tree", required=True)
    ap.add_argument("--probe-key", required=True, help="dedicated item the smoke claims and releases")
    ap.add_argument("--smoke-timeout", type=int, default=60)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="deploy and smoke the requested tree")
    mode.add_argument("--dry-run", action="store_true", help="print the plan (the default)")
    args = ap.parse_args(argv)
    if args.smoke_timeout <= 0:
        ap.error('--smoke-timeout must be positive')
    try:
        text = dropin_text(args.tag, args.sha, args.tree)
    except ValueError as exc:
        ap.error(str(exc))
    if not args.apply:
        print(text, end='')
        return 0
    code, msg = deploy(args.tag, args.sha, args.tree, args.probe_key, Session(json_mode=True),
                       timeout=args.smoke_timeout)
    print(msg, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
