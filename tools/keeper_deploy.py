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

Failed verification or smoke attempts restoration from the on-disk backup.
Only a complete restore permits rollback reload/restart; both outcomes are
reported independently. Failure exits 1 with the reason and recovery details
on stderr.  Environment as for ``pg``: PG_URL, PG_TOKEN_FILE.
"""
import argparse
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from plangraph.keeper.cli_verbs import RefusedCli, Session, Unreachable

UNIT = "plangraph-keeper.service"
DROPIN_DIR = Path.home() / ".config/systemd/user" / (UNIT + ".d")
MANAGED_DROPIN = "zzzz-plangraph-live-sync.conf"
SHOW = ("show", UNIT, "--property=WorkingDirectory", "--property=Environment")
RUNNING_SHOW = ("show", UNIT, "--property=MainPID")
PROC_ROOT = Path("/proc")


def systemctl(*args):
    """Run ``systemctl --user``; raise RuntimeError when it fails."""
    done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60, check=False)
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
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S-')
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
    tree = str(Path(tree).resolve())
    if any(c.isspace() or not c.isprintable() or c == '%' for c in tree):
        raise ValueError('resolved tree must contain no whitespace, control characters or percent signs')
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
    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(timeout, remaining) if remaining else timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if remaining:
            signal.setitimer(signal.ITIMER_REAL, max(remaining - (time.monotonic() - started), 1e-6), interval)


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
        remaining = deadline - clock()
        if epoch is not None and not released and remaining > 0:
            try:
                with smoke_deadline(min(remaining, 5)):
                    session.call("POST", "release", body={"key": probe_key, "epoch": epoch})
            except BaseException:  # noqa: BLE001, S110 - preserve the primary failure; lease bounds cleanup.
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


def verify_revision(tree, sha):
    """Bind an apply to the requested commit before backup or active-file writes."""
    if not re.fullmatch(r'[0-9a-fA-F]{7,64}', sha):
        raise RuntimeError('requested SHA must be a hexadecimal commit id (at least 7 digits)')
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    actual = subprocess.run(['git', '-C', tree, 'rev-parse', '--verify', 'HEAD'],
                            capture_output=True, text=True, check=True, timeout=60, env=env).stdout.strip()
    if not re.fullmatch(r'[0-9a-fA-F]{40,64}', actual) or not actual.lower().startswith(sha.lower()):
        raise RuntimeError('requested SHA does not match tree HEAD')


def main_pid(ctl):
    properties = dict(line.split('=', 1) for line in ctl(*RUNNING_SHOW).splitlines() if '=' in line)
    pid = properties.get('MainPID', '')
    if not pid.isdecimal() or int(pid) <= 0:
        raise RuntimeError('unit has no running MainPID')
    return pid


def verify_running(ctl, tree):
    """Check the actual process, including an absolute script pinned by ExecStart."""
    pid = main_pid(ctl)
    process = PROC_ROOT / pid
    try:
        cwd = (process / 'cwd').readlink()
        env = dict(entry.split(b'=', 1) for entry in (process / 'environ').read_bytes().split(b'\0') if b'=' in entry)
        argv = [os.fsdecode(arg) for arg in (process / 'cmdline').read_bytes().split(b'\0') if arg]
    except OSError:
        raise RuntimeError('cannot inspect running keeper process') from None
    if cwd.resolve() != Path(tree):
        raise RuntimeError('running cwd does not select the requested tree')
    if env.get(b'PYTHONPATH') != os.fsencode(str(Path(tree) / 'src')):
        raise RuntimeError('running PYTHONPATH does not select the requested tree')
    if not (argv[1:3] == ['-m', 'plangraph.keeper.daemon'] or len(argv) >= 2 and
            Path(argv[1]).resolve() == Path(tree) / 'src/plangraph/keeper/daemon.py'):
        raise RuntimeError('running ExecStart does not select the requested tree')
    if main_pid(ctl) != pid:
        raise RuntimeError('running MainPID changed during verification')


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
    verify_running(ctl, tree)


def deploy(tag, sha, tree, probe_key, session, dropin_dir=DROPIN_DIR, ctl=systemctl,
           timeout=60, sleep=time.sleep, clock=time.monotonic):
    """Return ``(0, "ok")`` or ``(1, reason)``; the previous drop-in set survives a failed smoke."""
    # Validate before backup or any mutation, including callers outside the CLI.
    text = dropin_text(tag, sha, tree)
    tree = str(Path(tree).resolve())
    with deployment_signals():
        return _deploy(text, sha, tree, probe_key, session, dropin_dir, ctl, timeout, sleep, clock)


def _deploy(text, sha, tree, probe_key, session, dropin_dir, ctl, timeout, sleep, clock):
    reason, touched, success = None, False, False
    note = None
    try:
        verify_revision(tree, sha)
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
    except BaseException as exc:  # noqa: BLE001 - interruptions must preserve rollback and its failure report.
        reason = str(exc) or type(exc).__name__
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if touched and not success:
            note = f"smoke failed: {reason}"
            restored = False
            try:
                restore_dropins(dropin_dir, snapshot_dropins(backup))
                restored = True
                note += "; previous drop-ins restored"
            except BaseException as exc:  # noqa: BLE001 - interruptions must preserve rollback and its failure report.
                note += f"; ROLLBACK FAILED: restore: {exc}; backup={backup}; reload/restart skipped"
            if restored:
                for action in (("daemon-reload",), ("restart", UNIT)):
                    try:
                        ctl(*action)
                        note += f"; rollback {action[0]} ok"
                    except BaseException as exc:  # noqa: BLE001 - interruptions must preserve rollback and its failure report.
                        note += f"; ROLLBACK FAILED: rollback {action[0]} failed: {exc}"
                try:
                    with smoke_deadline(timeout):
                        if not wait_health(session, clock() + timeout, sleep, clock):
                            note += "; ROLLBACK UNHEALTHY: health did not answer after restore"
                except BaseException as exc:  # noqa: BLE001 - interruptions must preserve rollback and its failure report.
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
