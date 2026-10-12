#!/usr/bin/env python3
"""M2B-ANCHOR: daily git tag of the newest anchor in the private data repo.

    anchor_tag.py --repo ~/code/<data-repo> --anchor-log /var/lib/pganchor/anchor.log [--push]

Tag ``anchor-YYYYMMDD`` (UTC day) on the data repo's HEAD, message = the anchor record
(seq, head, ts, reason).  An existing tag for the day is left alone (exit 0).  ``--push``
pushes only that tag to ``origin``.  Values are passed as argv lists (no shell).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from plangraph.keeper import anchor  # noqa: E402


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=check, timeout=60)


def main(argv=None, now=None) -> int:
    parser = argparse.ArgumentParser(prog="anchor_tag")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--anchor-log", required=True)
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser()
    last = anchor.latest(Path(args.anchor_log).expanduser())
    if last is None:
        print("anchor_tag: no anchor record yet", file=sys.stderr)
        return 1
    day = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    name = f"anchor-{day}"
    if _git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{name}", check=False).returncode == 0:
        print(f"anchor_tag: {name} exists")
        return 0
    message = json.dumps(last, sort_keys=True, separators=(",", ":"))
    try:
        _git(repo, "-c", "user.name=plangraph-keeper", "-c", "user.email=noreply@localhost",
             "tag", "-a", name, "-m", message)
        if args.push:
            _git(repo, "push", "origin", f"refs/tags/{name}")
    except subprocess.CalledProcessError as exc:
        print(f"anchor_tag: git failed ({exc.returncode}): {(exc.stderr or '').strip()[:200]}",
              file=sys.stderr)
        return 1
    print(f"anchor_tag: {name} seq {last['seq']}" + (" pushed" if args.push else ""))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
