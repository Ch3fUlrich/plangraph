#!/usr/bin/env python3
"""Fetch every keeper repo whose ref is a remote-tracking ref (origin/...).

Commit-gated closes verify a sha against the configured ref; without a fetch
the ref goes stale. Runs as ExecStartPre of plangraph-md-sync.service; never
fails the sync (exit 0, one line per repo on stderr).
"""
import json
import subprocess
import sys


def main(config_path):
    repos = json.load(open(config_path)).get("repos", {})
    for name, repo in sorted(repos.items()):
        ref = repo.get("ref") or ""
        if "/" not in ref:
            continue
        remote = ref.split("/", 1)[0]
        try:
            proc = subprocess.run(["git", "-C", repo["path"], "fetch", "--quiet", remote],
                                  capture_output=True, text=True, timeout=60)
            status = "ok" if proc.returncode == 0 else "rc=%d" % proc.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            status = type(exc).__name__
        print("fetch %s: %s" % (name, status), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
