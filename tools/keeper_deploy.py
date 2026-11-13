#!/usr/bin/env python3
"""Switch the live keeper to a new tree, smoke it, and roll back on failure.

    keeper_deploy.py --tag <tag> --sha <sha> --tree <worktree> --probe-key <key> [--dry-run]

Writes the systemd user drop-in ``live-sync-<tag>-<sha>.conf`` (WorkingDirectory
and PYTHONPATH of the new tree), runs daemon-reload + restart, then a live smoke
within ``--smoke-timeout`` seconds (default 60):

* ``/v1/health`` answers;
* claim + release of the dedicated probe item (the claim guard reads the
  origin decisions fresh, so "bad_state: cannot read decisions file" shows here);
* the ``close_merged`` route exists (any answer but not_found).

On any failure the previous drop-in set is restored byte for byte, then
daemon-reload, restart and a health check run, and the exit code is 1 with the
reason on stderr.  Environment as for ``pg``: PG_URL, PG_TOKEN_FILE.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from plangraph.keeper.cli_verbs import RefusedCli, Session, Unreachable  # noqa: E402

UNIT = "plangraph-keeper.service"
DROPIN_DIR = Path.home() / ".config/systemd/user" / (UNIT + ".d")


def systemctl(*args):
    """Run ``systemctl --user``; raise RuntimeError when it fails."""
    done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"systemctl {' '.join(args)}: {done.stderr.strip()}")


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


def dropin_text(tag, sha, tree):
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


def smoke(session, probe_key, timeout, sleep=time.sleep, clock=time.monotonic):
    """``None`` when the live keeper passes, else the reason string."""
    deadline = clock() + timeout
    if not wait_health(session, deadline, sleep, clock):
        return f"health did not answer within {timeout}s"
    try:
        claim = session.call("POST", "claim", body={"key": probe_key, "lease_s": 120})
        epoch = claim.get("epoch", (claim.get("item") or {}).get("epoch"))
        if epoch is None:
            return f"claim of {probe_key} returned no epoch"
        session.call("POST", "release", body={"key": probe_key, "epoch": epoch})
    except RefusedCli as exc:
        return f"claim/release of {probe_key} refused: {exc.code}: {exc.msg}"
    except Unreachable as exc:
        return f"claim/release of {probe_key} failed: {exc}"
    try:
        session.call("POST", "close_merged", body={})
    except RefusedCli as exc:
        if exc.code == "not_found":
            return "close_merged route missing"
    except Unreachable as exc:
        return f"close_merged route failed: {exc}"
    return None


def deploy(tag, sha, tree, probe_key, session, dropin_dir=DROPIN_DIR, ctl=systemctl,
           timeout=60, sleep=time.sleep, clock=time.monotonic):
    """Return ``(0, "ok")`` or ``(1, reason)``; the previous drop-in set survives a failed smoke."""
    saved = snapshot_dropins(dropin_dir)
    dropin_dir.mkdir(parents=True, exist_ok=True)
    (dropin_dir / f"live-sync-{tag}-{sha}.conf").write_text(dropin_text(tag, sha, tree))
    reason = None
    try:
        ctl("daemon-reload")
        ctl("restart", UNIT)
        reason = smoke(session, probe_key, timeout, sleep, clock)
    except RuntimeError as exc:
        reason = str(exc)
    if reason is None:
        return 0, "ok"
    restore_dropins(dropin_dir, saved)
    note = f"smoke failed: {reason}; previous drop-ins restored"
    try:
        ctl("daemon-reload")
        ctl("restart", UNIT)
        if not wait_health(session, clock() + timeout, sleep, clock):
            note += "; ROLLBACK UNHEALTHY: health did not answer after restore"
    except RuntimeError as exc:
        note += f"; ROLLBACK FAILED: {exc}"
    return 1, note


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--tree", required=True)
    ap.add_argument("--probe-key", required=True, help="dedicated item the smoke claims and releases")
    ap.add_argument("--smoke-timeout", type=int, default=60)
    args = ap.parse_args(argv)
    code, msg = deploy(args.tag, args.sha, args.tree, args.probe_key, Session(json_mode=True),
                       timeout=args.smoke_timeout)
    print(msg, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
