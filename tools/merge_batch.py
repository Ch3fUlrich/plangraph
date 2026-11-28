#!/usr/bin/env python3
"""Merge PRs in batches: one review, one merge and one full CI run per batch instead of per PR.

  plan      open PRs with a label (or --prs): head sha, CI on head, draft, files/lines, base.
            Stacks stay together (a PR goes after the PR it is based on); a PR touching an
            AUTH_DOOR/gate path of tools/ci_commit_gate.py or src/plangraph/keeper/ is HIGH (git
            adds the old path of a rename, from PR heads fetched from origin as objects only, so
            run plan in a checkout of the repo; without the gate file plan refuses); HIGH PRs fill
            HIGH batches apart from the standard ones, both cut by --max-prs and --max-lines. The
            batch-ready label overrides draft status; red CI and the needs-update label keep a PR out.
  build     branch batch/NAME in a new worktree from origin/<default branch>, `git merge --no-ff` each
            PR head as `Merge PR #N: <title>`. Every stack root must target the default branch (a PR
            stacked on a PR outside the batch is refused) and the plan rules apply to the set (red CI,
            needs-update, HIGH with standard PRs, caps) unless --force-set, which is logged in the
            state and the batch PR. A conflict stops with exit 3 (resolve, commit, run `continue`) or,
            with --skip-conflicts, skips the PR and labels it needs-update. Root .gitattributes sets default
            union paths; --union GLOB adds an
            append-only ledger (CHANGELOG.md) line by line instead; build warns when a PR changed old
            lines of such a path, because a union keeps the old text beside the new.
  continue  resume after the resolution commit, or after a crash: the state is saved before the
            worktree and before each merge, so a merge that was made is recorded, not lost.
  push      push batch/NAME, open the batch PR (table of PRs, Plan-Item values, skipped PRs,
            resolution commits), print its number. Run again after a fix to update it.
  review    record a keeper-bound review by run id, or a supplementary review file.
  go-req    print the batch GO request from saved reviews, CI and PLAN evidence.
  land      merge the batch PR only if its head is the pushed head, every required check is
            green and --approval ends in `HEAD: <sha>` (the batch head in full, all 40 hex
            digits) and `VERDICT: PASS` lines, with
            no FAIL/CHANGES verdict line anywhere. A batch of one part is refused (train_required) while
            another batch-ready PR is ready for main (plan exclusions, CI finished): ready PRs land as one train,
            and its parts need no GO row of their own. Then per included PR whose recorded head is an ancestor of the
            merge commit (else a problem, no MERGED line): MERGED -> `MERGED <pr> <sha> <item>`; open but in the merge -> closed as `merged via batch PR #N` (after its head is
            read once more), same line; a head that moved after the build -> `NOT-MERGED <pr> head
            moved`, not closed, needs-update. Cleanup then sweeps exact landed worktrees and
            SHA-leased branches; cleanup failures are printed without failing land.
  status    batches in progress, interrupted ones and orphans.  abort: drop a batch (worktree,
            branch, state; without a state file by branch name).

State lives in `<git common dir>/merge-batch/NAME.json`, so every worktree of the repo sees it.
Exit codes: 0 ok, 1 refused or failed, 2 usage, 3 conflict stop. Needs only git and gh.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import merge_batch_build as build
import merge_batch_land as land
from merge_batch_plan import high_patterns, plan_batches, render_table
from merge_batch_build import landing_journal, landing_lease
from merge_batch_util import (BATCH_READY, EXIT_REFUSED, MergeBatchError, check_name, drop_state, fetch_pr, gh, git, refresh_diff,
                              keeper_landing_configured, load_state, orphan_names, state_dir, state_path, worktrees,
                              ci_window, first_ready, gh_json, landing_report, parse_prs, record_landing, render_landing_report,
                              repo_info, require_landing_row, set_fix_tasks, settle_landings)


def cmd_plan(args):
    repo, default = repo_info(args.repo)
    base = args.base or default
    if not base:
        raise MergeBatchError(f"{repo} has no default branch: pass --base")
    patterns = high_patterns(args.high_path)
    if args.prs:
        prs = [fetch_pr(repo, num) for num in parse_prs(args.prs)]
        if land.keeper_landing_configured():
            prs = land.exclude_consumed(repo, prs)
    else:
        prs = build.labelled_prs(repo, args.label)
    refresh_diff(base, prs)  # Git owns sizes and paths, including both ends of renames
    plan = plan_batches(prs, base, args.max_prs, args.max_lines, patterns)
    plan.update(repo=repo, label=None if args.prs else args.label)
    print(json.dumps(plan, indent=2) if args.json else render_table(plan))
    return 0


def cmd_abort(args):
    """Drop a batch: its worktree, branch, PR-head refs and state. Without a state file the leftovers of a
    build are found by branch name (batch/NAME), but only when the build's PR-head refs mark them as ours."""
    name = check_name(args.name)
    if state_path(name).exists():
        st = load_state(name)
    elif name in orphan_names():
        st = {"name": name, "branch": f"batch/{name}", "worktree": "", "pr": None}
        print(f"batch {name!r} has no state file: removing what its build left", file=sys.stderr)
    else:
        raise MergeBatchError(f"no batch {name!r} in progress (merge_batch.py status lists them)")
    if st.get("pr") and keeper_landing_configured():
        with landing_lease(st["repo"]) as lease:
            train = landing_journal(st["repo"], "show", pr=st["pr"])["train"]
            if train is not None and not train["merge"]:
                landing_journal(st["repo"], "abandon", pr=st["pr"], **lease)
    os.chdir(state_dir().parent)  # the git dir: the cwd may be the worktree that is about to go
    registered = dict(worktrees())
    paths = {path for path, branch in registered.items() if branch == st["branch"]}
    if st.get("worktree") in registered:
        paths.add(st["worktree"])
    for wt in sorted(paths):
        if Path(wt).is_dir():  # a directory removed by hand: worktree remove/prune clean its admin entry
            git("merge", "--abort", cwd=wt, ok=None)
        git("worktree", "remove", "--force", "--force", wt)
    git("worktree", "prune", ok=None)
    if st.get("worktree") and Path(st["worktree"]).exists():
        print(f"warning: {st['worktree']} is no worktree of this repo: left in place", file=sys.stderr)
    git("branch", "-D", st["branch"], ok=None)
    drop_state(st)
    note = f"; batch PR #{st['pr']} is still open on the remote" if st.get("pr") else ""
    print(f"aborted batch {st['name']}: removed {', '.join(sorted(paths)) or 'no worktree'} and {st['branch']}{note}")
    return 0


def cmd_landing_mode(args):
    """Print the landing mode of ``--repo`` (default: gh's current repository) from the ``landing.mode`` setting:
    the one reading every landing path uses (router merge-part calls this). An unknown mode exits 1."""
    from plangraph.settings import SettingsError, landing_mode

    try:
        print(landing_mode(args.repo or repo_info()[0]))
    except SettingsError as exc:
        raise MergeBatchError(f"landing mode refused: {exc}") from None
    return 0


def cmd_landing_report(args):
    """Compare the landing modes from the telemetry database in one command: trains, median and worst minutes from
    the first part READY to landed and from landed to the first green run, test minutes per landed part, CI
    reruns, red-main count and fix tasks per train (``--json`` for the same numbers as JSON)."""
    report = landing_report()
    print(json.dumps(report, indent=2) if args.json else render_landing_report(report), end="" if not args.json else "\n")
    return 0


def cmd_landing_settle(args):
    """Fill the green and red-main columns of the telemetry rows from the runs GitHub shows on each landed commit;
    ``--fix-tasks N`` records how many fix tasks named the train ``--train`` (a repository's repair work)."""
    if args.train:
        if args.fix_tasks is None:
            raise MergeBatchError("--train needs --fix-tasks N")
        set_fix_tasks(args.train, args.fix_tasks)
    done = settle_landings(lambda repo, sha: gh_json(
        "api", f"repos/{repo}/actions/workflows/tests.yml/runs?head_sha={sha}&event=push&per_page=100")["workflow_runs"])
    print(f"settled {done} landing row(s)")
    return 0


def land_with_full_run(args):
    """Land the batch PR. The full suite runs once, at the merge into main: when the batch head has no green full
    run yet, label the batch PR ci-full (CI starts the full run on that event) and refuse until it is green."""
    try:
        return land.cmd_land(args)
    except MergeBatchError as exc:
        if not str(exc).startswith("full_ci: ") or "must target main" in str(exc):
            raise
        gh("pr", "edit", str(args.batch_pr), "--repo", load_state(args.name)["repo"], "--add-label", "ci-full")
        raise MergeBatchError(f"{exc}; labelled ci-full: the full suite runs once now, run land again when it is green") from exc


def cmd_landing_backfill(args):
    """Record a train pull request that was landed before the telemetry existed, from what GitHub shows: its
    creation (train build start), merge time and merge commit, the full run on its head (times, run id,
    reruns) and the rows of the first table of its description (the parts). First-READY and GO times
    are not on GitHub: pass ``--ready-at`` and ``--go-at`` when the keeper log knows them, else they stay unknown
    and the statistics skip them. ``landing-settle`` then fills the green and red-main columns."""
    repo, _ = repo_info(args.repo)
    pr = gh_json("pr", "view", str(args.pr), "--repo", repo, "--json",
                 "number,title,body,createdAt,mergedAt,mergeCommit,headRefOid,state")
    if pr["state"] != "MERGED" or not (pr.get("mergeCommit") or {}).get("oid"):
        raise MergeBatchError(f"PR #{args.pr} is {pr['state']}: only a merged train is backfilled")
    table = [line for line in (pr.get("body") or "").splitlines() if line.startswith("|")]
    rows = [[c.strip() for c in line.strip("|").split("|")] for line in table[2:]]
    parts = [int(m.group(1)) if (m := re.search(r"#(\d+)", " ".join(r))) else r[0] for r in rows if r and r[0]]
    window = ci_window(repo, pr["headRefOid"])
    row = {"repo": repo, "mode": args.mode, "train": f"pr{args.pr}", "pr": args.pr, "parts": parts,
           "landed_sha": pr["mergeCommit"]["oid"], "first_ready_at": args.ready_at, "build_started_at": pr["createdAt"],
           "go_at": args.go_at, "landed_at": pr["mergedAt"], "fit_commits": None, "left_out": None, **window}
    record_landing(row, fill_only=True)
    print(f"recorded {args.mode} train #{args.pr}: {len(parts)} part(s), landed {pr['mergedAt']}")
    return 0


def cmd_landing_record(args):
    """Write the telemetry row of a landing another tool made (the router's merge-part): repo, mode, train key,
    the pull request, the landed commit and the head whose full run counts as the test window. The row is read
    back; a row that cannot be written exits 1, so the caller can fail the landing."""
    from datetime import datetime, timezone

    window = ci_window(args.repo, args.head) if args.head else {}
    parts = [int(n) for n in args.parts.split(",") if n]
    row = {"repo": args.repo, "mode": args.mode, "train": args.train, "pr": args.pr, "parts": parts,
           "landed_sha": args.sha, "first_ready_at": first_ready(args.repo, [{"pr": n} for n in parts]),
           "landed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **window}
    if args.mode == "tested-ff":
        row["green_at"] = window.get("ci_ended_at")
    try:
        record_landing(row)
        require_landing_row(args.repo, args.train, args.sha, args.mode)
    except (sqlite3.Error, OSError) as exc:
        raise MergeBatchError(f"landing telemetry row not written: {exc}") from None
    print(f"landing row recorded: mode={args.mode} sha={args.sha}")
    return 0


def positive(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def set_options(cmd):
    """The plan rules, as options of plan and of build (which applies them to its PRs)."""
    cmd.add_argument("--max-prs", type=positive, default=6)
    cmd.add_argument("--max-lines", type=positive, default=1200, help="added + deleted lines per batch")
    cmd.add_argument("--high-path", action="append", default=[], metavar="GLOB",
                     help="extra path glob that makes a PR HIGH (repeatable)")


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
    cmd.add_argument("--base", help="batch base branch (default: the repo default branch)")
    set_options(cmd)
    cmd.add_argument("--json", action="store_true")
    cmd = add("build", build.cmd_build, "merge PRs into batch/NAME", repo=True)
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--prs", required=True, help="comma separated PR numbers, in merge order")
    cmd.add_argument("--plan", help="keeper key for the batch PLAN block")
    cmd.add_argument("--deviations", default="none", help="batch plan deviations (default: none)")
    cmd.add_argument("--skip-conflicts", action="store_true", help="skip a conflicting PR and label it needs-update")
    set_options(cmd)
    cmd.add_argument("--force-set", action="store_true",
                     help="build PRs that break the plan rules (red CI, needs-update, HIGH with standard PRs, caps); "
                          "logged in the state and the batch PR")
    cmd.add_argument("--worktree", help="worktree path (default: <repo>-worktrees/batch-NAME)")
    cmd.add_argument("--union", action="append", default=[], metavar="GLOB",
                     help="add to the fetched base .gitattributes union defaults; merge this path line by line (git merge=union) instead of conflicting, "
                          "for append-only ledgers such as CHANGELOG.md (repeatable)")
    cmd = add("continue", build.cmd_continue, "resume after a conflict resolution commit")
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--skip-conflicts", action="store_true")
    cmd = add("push", land.cmd_push, "push batch/NAME and open the batch PR")
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--ci-run", type=positive, help="full batch CI run id; repeat push after CI completes")
    cmd = add("review", land.cmd_review, "record a review on a part's full head")
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--pr", required=True, type=positive)
    evidence = cmd.add_mutually_exclusive_group(required=True)
    evidence.add_argument("--run-id", help="keeper-bound spawner review run")
    evidence.add_argument("--file", help="supplementary review file")
    add("go-req", land.cmd_go_req, "print the batch GO request").add_argument("--name", required=True)
    cmd = add("land", land_with_full_run, "merge the batch PR and report the included PRs")
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--batch-pr", required=True, type=int)
    cmd.add_argument("--approval", metavar="PATH",
                     help="seat verdict text ending in 'HEAD: <the batch head sha in full>' and "
                          "'VERDICT: PASS' lines")
    cmd.add_argument("--go-row", metavar="PATH", help="recorded GO row; required for a keeper migration or a red-main exception")
    cmd.add_argument("--checks", metavar="NAME,..", help="check names to require instead of the required checks")
    cmd.add_argument("--wait", type=float, default=20.0, help="seconds to wait for GitHub to mark PRs merged")
    cmd.add_argument("--subject", metavar="TEXT",
                     help="merge commit subject (must end with '(#N)' for the batch PR)")
    add("landing-mode", cmd_landing_mode, "print the landing.mode setting of a repository", repo=True)
    cmd = add("landing-report", cmd_landing_report, "compare the landing modes from the telemetry database")
    cmd.add_argument("--json", action="store_true")
    cmd = add("landing-settle", cmd_landing_settle, "fill green/red-main columns of the telemetry rows")
    cmd.add_argument("--train", help="train name whose fix-task count to record")
    cmd.add_argument("--fix-tasks", type=int, help="fix tasks that named --train")
    cmd = add("landing-backfill", cmd_landing_backfill, "record a past landed train pull request as a telemetry row",
              repo=True)
    cmd.add_argument("--pr", required=True, type=positive, help="the merged train pull request")
    cmd.add_argument("--mode", required=True, choices=("tested-ff", "train-pr-merge", "single-pr"))
    cmd.add_argument("--ready-at", help="ISO time the first part was READY, when known")
    cmd.add_argument("--go-at", help="ISO time of the GO, when known")
    cmd = add("landing-record", cmd_landing_record, "write the telemetry row of a landing made by another tool")
    cmd.add_argument("--repo", required=True)
    cmd.add_argument("--mode", required=True, choices=("tested-ff", "train-pr-merge", "single-pr"))
    cmd.add_argument("--train", required=True)
    cmd.add_argument("--pr", required=True, type=positive)
    cmd.add_argument("--sha", required=True, help="the commit that is main's tip after the landing")
    cmd.add_argument("--head", help="the pull request head whose full run is the test window")
    cmd.add_argument("--parts", default="", help="comma separated part pull request numbers")
    add("settle", land.cmd_settle, "resume pending keeper landing settlement in this repository")
    cmd = add("consumed", land.cmd_consumed, "check whether this exact PR head landed with a train", repo=True)
    cmd.add_argument("--pr", required=True, type=positive)
    cmd.add_argument("--head", required=True)
    add("status", land.cmd_status, "batches in progress").add_argument("--json", action="store_true")
    add("abort", cmd_abort, "drop a batch: worktree, branch and state").add_argument("--name", required=True)
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
