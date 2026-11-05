"""Retarget merged stacks and report missing PR CI; no scheduler is installed."""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime


def run(argv, **kwargs):
    return subprocess.run(argv, text=True, capture_output=True, **kwargs)


class Failure(Exception):
    pass


class CI:
    def __init__(self, repo, runner=run, clock=time.monotonic, sleep=time.sleep):
        self.repo, self.runner = repo, runner
        self.clock, self.sleep = clock, sleep

    def call(self, argv, **kwargs):
        result = self.runner(argv, **kwargs)
        if result.returncode:
            raise Failure((result.stderr.strip() or "command failed").replace("\n", " "))
        return result.stdout.strip()

    def gh(self, *args):
        return self.call(["gh", *args, "--repo", self.repo])

    def api(self, endpoint):
        return json.loads(self.call(["gh", "api", f"repos/{self.repo}/{endpoint}"]))

    def merged_base(self, branch):
        prs = json.loads(self.gh("pr", "list", "--state", "merged", "--head", branch,
                                 "--json", "headRefName", "--limit", "100"))
        return any(p["headRefName"] == branch for p in prs)

    def checks(self, sha):
        return self.api(f"commits/{sha}/check-runs")["total_count"]

    def wait(self, sha, seconds):
        end = self.clock() + seconds
        while True:
            count = self.checks(sha)
            if count or self.clock() >= end:
                return count
            self.sleep(min(2, max(0, end - self.clock())))

    def retarget(self, args):
        pr = json.loads(self.gh("pr", "view", str(args.pr), "--json",
                                "baseRefName,headRefName"))
        branch, base = pr["headRefName"], pr["baseRefName"]
        if base != "main" and not self.merged_base(base):
            raise Failure("base branch does not belong to a merged PR")
        def git(*argv):
            return self.call(["git", *argv], cwd=args.checkout)
        if git("branch", "--show-current") != branch:
            raise Failure("checkout is on another branch")
        if git("status", "--porcelain"):
            raise Failure("checkout is dirty")
        before = git("rev-parse", "HEAD")
        if base != "main":
            self.gh("pr", "edit", str(args.pr), "--base", "main")
        git("fetch", "origin", "main")
        result = self.runner(["git", "merge", "--no-edit", "origin/main"], cwd=args.checkout)
        if result.returncode:
            files = git("diff", "--name-only", "--diff-filter=U")
            git("merge", "--abort")
            if not files:
                raise Failure("merge failed without conflicting files")
            print(alert(args.pr, before, "conflicting", ", ".join(files.splitlines())))
            return 3
        head = git("rev-parse", "HEAD")
        if head != before:
            git("push", "origin", f"HEAD:{branch}")
        count = self.wait(head, args.wait_s)
        if not count:
            self.gh("pr", "close", str(args.pr))
            self.gh("pr", "reopen", str(args.pr))
            count = self.wait(head, args.wait_s)
        if not count:
            print(alert(args.pr, head, "no_check_run", "no check run after reopen"))
            return 4
        print(f"RETARGETED #{args.pr} {head[:12]} {count}")
        return 0

    def check(self, args):
        path = Path(args.state)
        old = {tuple(row) for row in json.loads(path.read_text())} if path.exists() else set()
        prs = json.loads(self.gh("pr", "list", "--state", "open", "--limit", "100000",
                                "--json", "number,headRefOid,baseRefName,mergeable,createdAt"))
        findings = []
        for pr in prs:
            number, head, base = pr["number"], pr["headRefOid"], pr["baseRefName"]
            if pr["mergeable"] == "CONFLICTING":
                findings.append((number, head, "conflicting", "merge conflict"))
            if base != "main":
                if self.merged_base(base):
                    findings.append((number, head, "base_merged", f"base {base} merged"))
            elif not self.checks(head):
                commit = self.api(f"commits/{head}")
                newest = max(timestamp(pr["createdAt"]),
                             timestamp(commit["commit"]["committer"]["date"]))
                if time.time() - newest > args.grace_s:
                    findings.append((number, head, "no_check_run", "head has no check run"))
        # Finish every read before emitting alerts or replacing state.
        opened = {p["number"] for p in prs}
        saved = {key for key in old if key[0] in opened}
        fresh = False
        for number, head, cause, detail in findings:
            key = (number, head, cause)
            if key in saved:
                continue
            fresh = True
            line = alert(number, head, cause, detail)
            print(line)
            if args.alert_cmd:
                try:
                    self.call(shlex.split(args.alert_cmd), input=line + "\n")
                except (Failure, OSError) as exc:
                    print(f"alert delivery failed: {exc}", file=sys.stderr)
                    continue
            saved.add(key)
        atomic_state(path, sorted(saved))
        return int(fresh)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def alert(number, head, cause, detail):
    return f"ALERT #{number} {head[:12]} {cause} {' '.join(detail.split())}"


def atomic_state(path, rows):
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            name = stream.name
            os.chmod(name, 0o600)
            json.dump(rows, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def main(argv=None, runner=run, clock=time.monotonic, sleep=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    retarget = commands.add_parser("retarget")
    retarget.add_argument("--pr", type=int, required=True)
    retarget.add_argument("--checkout", required=True)
    retarget.add_argument("--wait-s", type=float, default=120)
    check = commands.add_parser("check")
    check.add_argument("--state", required=True)
    check.add_argument("--alert-cmd")
    check.add_argument("--grace-s", type=float, default=300)
    for command in (retarget, check):
        command.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    try:
        ci = CI(args.repo, runner, clock, sleep)
        return getattr(ci, args.command)(args)
    except (Failure, OSError, ValueError, KeyError) as exc:
        print("ERROR " + " ".join(str(exc).split()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
