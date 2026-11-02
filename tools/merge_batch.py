#!/usr/bin/env python3
"""Merge PRs in batches: one review, one merge and one full CI run per batch instead of per PR.

  plan      open PRs with a label (or --prs): head sha, CI on head, draft, files/lines, base.
            Stacks stay together (a PR goes after the PR it is based on); a PR touching an
            AUTH_DOOR/gate path of tools/ci_commit_gate.py or src/plangraph/keeper/ is HIGH;
            HIGH PRs fill HIGH batches apart from the standard ones, both cut by --max-prs and
            --max-lines. The batch-ready label overrides draft status; red CI and the
            needs-update label keep a PR out.
  build     branch batch/NAME in a new worktree from origin/<base>; `git merge --no-ff` each PR
            head as `Merge PR #N: <title>`. A conflict stops with exit 3 (resolve, commit, run
            `continue`) or, with --skip-conflicts, skips the PR and labels it needs-update.
            --union GLOB merges an append-only ledger (CHANGELOG.md) line by line instead.
  continue  resume after the resolution commit.
  push      push batch/NAME, open the batch PR (table of PRs, Plan-Item values, skipped PRs,
            resolution commits), print its number. Run again after a fix to update it.
  land      merge the batch PR only if its head is the pushed head, every required check is
            green and --approval ends in `HEAD: <sha>` (that head) and `VERDICT: PASS` lines, with
            no FAIL/CHANGES verdict line anywhere; then check each included PR is MERGED (else
            close it as `merged via batch PR #N`) and print `MERGED <pr> <sha> <item>`.
  status    batches in progress.  abort: drop a batch (worktree, branch, state).

State lives in `<git common dir>/merge-batch/NAME.json`, so every worktree of the repo sees it.
Exit codes: 0 ok, 1 refused or failed, 2 usage, 3 conflict stop. Needs only git and gh.
"""
from __future__ import annotations

import argparse
import json
import sys

import merge_batch_build as build
import merge_batch_land as land
from merge_batch_plan import high_patterns, plan_batches, render_table
from merge_batch_util import (BATCH_READY, EXIT_REFUSED, PR_FIELDS, MergeBatchError, fetch_pr, gh_json, norm_pr,
                              parse_prs, repo_info)


def cmd_plan(args):
    repo, default = repo_info(args.repo)
    base = args.base or default
    if not base:
        raise MergeBatchError(f"{repo} has no default branch: pass --base")
    patterns = high_patterns(args.high_path)
    if args.prs:
        prs = [fetch_pr(repo, num) for num in parse_prs(args.prs)]
    else:
        raw = gh_json("pr", "list", "--repo", repo, "--state", "open", "--label", args.label,
                      "--limit", "200", "--json", PR_FIELDS)
        prs = [norm_pr(item) for item in raw]
    plan = plan_batches(prs, base, args.max_prs, args.max_lines, patterns)
    plan.update(repo=repo, label=None if args.prs else args.label)
    print(json.dumps(plan, indent=2) if args.json else render_table(plan))
    return 0


def positive(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def parser():
    top = argparse.ArgumentParser(prog="merge_batch.py", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="cmd", required=True)

    def add(name, func, text, repo=False):
        cmd = sub.add_parser(name, help=text)
        if repo:  # later commands use the repo saved in the batch state
            cmd.add_argument("--repo", help="OWNER/NAME (default: gh repo view)")
        cmd.set_defaults(func=func)
        return cmd

    cmd = add("plan", cmd_plan, "list batch candidates and cut batches", repo=True)
    cmd.add_argument("--label", default=BATCH_READY, help=f"PR label to select (default {BATCH_READY})")
    cmd.add_argument("--prs", help="comma separated PR numbers instead of the label")
    cmd.add_argument("--max-prs", type=positive, default=6)
    cmd.add_argument("--max-lines", type=positive, default=1200, help="added + deleted lines per batch")
    cmd.add_argument("--base", help="batch base branch (default: the repo default branch)")
    cmd.add_argument("--high-path", action="append", default=[], metavar="GLOB",
                     help="extra path glob that makes a PR HIGH (repeatable)")
    cmd.add_argument("--json", action="store_true")
    cmd = add("build", build.cmd_build, "merge PRs into batch/NAME", repo=True)
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--prs", required=True, help="comma separated PR numbers, in merge order")
    cmd.add_argument("--skip-conflicts", action="store_true", help="skip a conflicting PR and label it needs-update")
    cmd.add_argument("--worktree", help="worktree path (default: <repo>-worktrees/batch-NAME)")
    cmd.add_argument("--union", action="append", default=[], metavar="GLOB",
                     help="merge this path line by line (git merge=union) instead of conflicting, "
                          "for append-only ledgers such as CHANGELOG.md (repeatable)")
    cmd = add("continue", build.cmd_continue, "resume after a conflict resolution commit")
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--skip-conflicts", action="store_true")
    add("push", land.cmd_push, "push batch/NAME and open the batch PR").add_argument("--name", required=True)
    cmd = add("land", land.cmd_land, "merge the batch PR and report the included PRs")
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--batch-pr", required=True, type=int)
    cmd.add_argument("--approval", metavar="PATH",
                     help="seat verdict text ending in 'HEAD: <batch head sha>' and 'VERDICT: PASS' lines")
    cmd.add_argument("--checks", metavar="NAME,..", help="check names to require instead of the required checks")
    cmd.add_argument("--wait", type=float, default=20.0, help="seconds to wait for GitHub to mark PRs merged")
    add("status", land.cmd_status, "batches in progress").add_argument("--json", action="store_true")
    add("abort", build.cmd_abort, "drop a batch: worktree, branch and state").add_argument("--name", required=True)
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except MergeBatchError as exc:
        print(f"merge_batch: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
