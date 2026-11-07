#!/usr/bin/env python3
"""Run pinned CI commands on an SSH host with preinstalled CI prerequisites.

The remote login starts in the repository. origin/main is the PR comparison
base. No dependencies are installed and no remote output/environment is printed.
Postgres and Rust table entries intentionally defer to slice 2.
Requested deferred jobs yield partial (exit 2); failures yield exit 1.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid


JOBS = {
    "select": ("python3 tools/ci_select.py select --base origin/main --head HEAD", 600),
    "scrub": ("python3 tools/split/scrub_check.py . --rules tools/split/manifest.toml "
              "--rules tools/split/scrub_ci.toml", 900),
    "file-size": ("FILESIZE_REQUIRE_BASE=origin/main python3 tools/check_file_size.py", 600),
    "changelog-docs": ("CHANGELOG_BASE=origin/main python3 tools/check_changelog_docs.py", 600),
    "pytest": ("python3 tools/affected_tests.py --base origin/main --run -n auto", 1200),
    # Service provisioning and Rust setup belong to slice 2.
    "pytest-postgres": (None, 0),
    "rust": (None, 0),
    "rust-worker": (None, 0),
    "rust-spike": (None, 0),
}


def remote(host, command, timeout=120, runner=subprocess.run):
    """The single SSH boundary; a subprocess-compatible runner is injectable."""
    return runner(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                   "--", host, command], capture_output=True, text=True,
                  timeout=timeout, check=False)


def main(argv=None, *, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--host", default="dell-build")
    parser.add_argument("--jobs", default="scrub,file-size,changelog-docs,pytest")
    parser.add_argument("--out", type=Path, default=Path("out/dell-ci"))
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.sha):
        parser.error("--sha must be exactly 40 hexadecimal characters")
    names = args.jobs.split(",")
    if any(name not in JOBS for name in names):
        parser.error("unknown job")
    if not re.fullmatch(r"[a-zA-Z0-9_.@-]+", args.host) or args.host.startswith("-"):
        parser.error("invalid host")
    run_id = uuid.uuid4().hex
    # A random private directory avoids collisions with other runs and user trees.
    directory = f"/tmp/dell-ci-{run_id}"
    tree = directory + "/worktree"
    qtree = shlex.quote(tree)
    record = {"sha": args.sha, "host": args.host,
              "started": datetime.now(timezone.utc).isoformat(),
              "jobs": [], "not_requested": [name for name in JOBS if name not in names],
              "result": "failure"}

    def call(command, timeout=120):
        return remote(args.host, command, timeout, runner)

    def checked(command):
        result = call(command)
        if result.returncode:
            raise RuntimeError("remote setup failed")
        return result.stdout

    try:
        checked(f"git fetch origin && mkdir -m 700 {directory} && "
                f"git worktree add --detach {qtree} {args.sha}")
        head = checked(f"git -C {qtree} rev-parse HEAD").strip()
        if head != args.sha.lower():
            raise RuntimeError("HEAD mismatch")
        if checked(f"git -C {qtree} status --porcelain --untracked-files=all"):
            raise RuntimeError("dirty worktree")
        record["result"] = "success"
        for name in names:
            command, timeout = JOBS[name]
            job = {"job": name, "result": "skipped", "duration_s": 0, "exit": None}
            record["jobs"].append(job)
            if command is None:
                job["reason"] = "slice 2"
                if record["result"] == "success":
                    record["result"] = "partial"
                continue
            start = time.monotonic()
            try:
                # GNU timeout kills the remote job even if the SSH client disconnects.
                script = f"cd {qtree} && export PYTHONPATH={qtree}/src && {command}"
                result = call(f"timeout --kill-after=10s {timeout}s sh -c "
                              f"{shlex.quote(script)}", timeout + 30)
                job["exit"] = result.returncode
                job["result"] = ("timeout" if result.returncode == 124 else
                                 "success" if result.returncode == 0 else "failure")
            except subprocess.TimeoutExpired:
                job["result"] = "timeout"
            except OSError:
                job["result"] = "failure"
            finally:
                job["duration_s"] = round(time.monotonic() - start, 3)
            if job["result"] != "success":
                record["result"] = "failure"
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        record["result"] = "failure"
        record["reason"] = "remote setup or verification failed"
    finally:
        try:
            result = call(f"git worktree remove --force {qtree} && rmdir {directory}")
            if result.returncode:
                record["result"] = "failure"
                record["cleanup"] = "failure"
        except (OSError, subprocess.TimeoutExpired):
            record["result"] = "failure"
            record["cleanup"] = "failure"
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"{run_id}.json").write_text(json.dumps(record, indent=2) + "\n")
    return {"success": 0, "failure": 1, "partial": 2}[record["result"]]


if __name__ == "__main__":
    raise SystemExit(main())
