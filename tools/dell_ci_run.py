#!/usr/bin/env python3
"""Run pinned CI commands on an SSH host with preinstalled CI prerequisites.

The remote repository is selected explicitly. origin/main is the PR comparison
base. No dependencies are installed and no remote output/environment is printed.
Services and build products are disposable; failures yield exit 1.
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
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dell_ci_jobs import script as service_script


JOBS = {
    "select": ("python3 tools/ci_select.py select --full", 600),
    "scrub": ("python3 tools/split/scrub_check.py . --rules tools/split/manifest.toml "
              "--rules tools/split/scrub_ci.toml", 900),
    "file-size": ("FILESIZE_REQUIRE_BASE=origin/main python3 tools/check_file_size.py", 600),
    "changelog-docs": ("CHANGELOG_BASE=origin/main python3 tools/check_changelog_docs.py", 600),
    "pytest": ("python3 -m pytest -q -n auto --dist loadgroup --durations=30 tests", 1200),
    "pytest-postgres": ("service", 2400),
    "rust": ("service", 2700),
    "rust-worker": ("service", 2700),
    "rust-spike": ("service", 1800),
    "browser": ("python3 -m pytest -q tests/browser -m browser", 900),
    "ci-gate": ("gate", 120),
}

OUTPUT_TAIL_LIMIT = 8192


def output_tail(stdout=None, stderr=None):
    """Retain bounded diagnostics, including partial output on SSH timeout."""
    def text(value):
        return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
    return (text(stdout)[-OUTPUT_TAIL_LIMIT:] + "\n" +
            text(stderr)[-OUTPUT_TAIL_LIMIT:])[-OUTPUT_TAIL_LIMIT:]


def remote(host, command, timeout=120, runner=subprocess.run):
    """The single SSH boundary; a subprocess-compatible runner is injectable."""
    return runner(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                   "--", host, command], capture_output=True, text=True,
                  timeout=timeout, check=False)


def main(argv=None, *, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--host", default="dell-build")
    parser.add_argument("--repo", default="~/code/plangraph")
    parser.add_argument("--jobs", default=",".join(JOBS))
    parser.add_argument("--out", type=Path, default=Path("out/dell-ci"))
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.sha):
        parser.error("--sha must be exactly 40 hexadecimal characters")
    names = args.jobs.split(",")
    if any(name not in JOBS for name in names):
        parser.error("unknown job")
    if not re.fullmatch(r"[a-zA-Z0-9_.@-]+", args.host) or args.host.startswith("-"):
        parser.error("invalid host")
    if not re.fullmatch(r"(?:~/|/)[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_.-]+)*", args.repo):
        parser.error("invalid repository path")
    repo = ('"$HOME"/' + shlex.quote(args.repo[2:])
            if args.repo.startswith("~/") else shlex.quote(args.repo))
    git = f"git -C {repo}"
    run_id = uuid.uuid4().hex
    # A random private directory avoids collisions with other runs and user trees.
    directory = f'"$HOME"/.cache/dell-ci/{run_id}'
    tree = directory + "/worktree"
    qtree = tree  # Keep $HOME expansion on the remote, including paths with spaces.
    record = {"sha": args.sha, "host": args.host,
              "started": datetime.now(timezone.utc).isoformat(),
              "jobs": [], "not_requested": [name for name in JOBS if name not in names],
              "result": "failure"}

    def call(command, timeout=120):
        return remote(args.host, command, timeout, runner)

    def checked(command, reason):
        result = call(command)
        if result.returncode:
            raise RuntimeError(reason)
        return result.stdout

    try:
        checked(f"{git} fetch origin", "origin fetch failed")
        checked(f"{git} fetch origin {args.sha}", "requested SHA fetch failed")
        checked(f'mkdir -p "$HOME"/.cache/dell-ci && mkdir -m 700 {directory} && '
                f"{git} worktree add --detach {qtree} {args.sha}",
                "worktree setup failed")
        head = checked(f"git -C {qtree} rev-parse HEAD", "HEAD verification failed").strip()
        if head != args.sha.lower():
            raise RuntimeError("HEAD mismatch")
        if checked(f"git -C {qtree} status --porcelain --untracked-files=all",
                   "worktree status failed"):
            raise RuntimeError("dirty worktree")
        record["result"] = "success"
        for name in names:
            command, timeout = JOBS[name]
            job = {"job": name, "result": "skipped", "duration_s": 0, "exit": None,
                   "output_tail": ""}
            record["jobs"].append(job)
            start = time.monotonic()
            try:
                if name.startswith("rust"):
                    space = call(f"df -Pk {directory}")
                    if space.returncode:
                        job["reason"] = "df command failed"
                    else:
                        try:
                            available = int(space.stdout.splitlines()[-1].split()[3])
                        except (ValueError, IndexError):
                            job["reason"] = "cannot parse df output"
                        else:
                            if available < 20971520:
                                job["reason"] = "insufficient ~/.cache/dell-ci space: requires at least 20 GB free"
                    if "reason" in job:
                        job["result"] = "failure"
                        record["result"] = "failure"
                        continue
                if command == "service":
                    command = service_script(name, directory + "/" + name)
                if command == "gate":
                    needs = {j["job"]: {"result": j["result"]}
                             for j in record["jobs"] if j["job"] != "ci-gate"}
                    command = ("python3 tools/ci_select.py gate --needs " +
                               shlex.quote(json.dumps(needs)))
                # GNU timeout kills the remote job even if the SSH client disconnects.
                script = f"cd {qtree} && export PYTHONPATH={qtree}/src && {command}"
                result = call(f"timeout --kill-after=10s {timeout}s sh -c "
                              f"{shlex.quote(script)}", timeout + 30)
                job["exit"] = result.returncode
                job["output_tail"] = output_tail(result.stdout, result.stderr)
                job["result"] = ("timeout" if result.returncode == 124 else
                                 "success" if result.returncode == 0 else "failure")
            except subprocess.TimeoutExpired as exc:
                job["output_tail"] = output_tail(exc.stdout, exc.stderr)
                job["result"] = "timeout"
            except OSError as exc:
                job["output_tail"] = str(exc)[-OUTPUT_TAIL_LIMIT:]
                job["result"] = "failure"
            finally:
                job["duration_s"] = round(time.monotonic() - start, 3)
            if job["result"] != "success":
                record["result"] = "failure"
    except RuntimeError as exc:
        record["result"] = "failure"
        record["reason"] = str(exc)
    except (OSError, subprocess.TimeoutExpired):
        record["result"] = "failure"
        record["reason"] = "remote setup or verification failed"
    finally:
        try:
            result = call(f"{git} worktree remove --force {qtree}; "
                           f"removed=$?; rm -rf -- {directory}; "
                          'cleaned=$?; test "$removed" -eq 0 && test "$cleaned" -eq 0')
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
