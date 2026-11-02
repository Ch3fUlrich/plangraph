"""build / continue / abort for tools/merge_batch.py: merge PR heads into batch/NAME with --no-ff.

The state file is saved before every step that changes the repo (the worktree, each merge), so a crash
leaves a marker that continue and abort act on: status ``creating`` (the worktree may be half made) or a
``merging`` PR (its merge commit may exist already)."""
from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

from merge_batch_plan import high_patterns, plan_batches, set_problems, stack_parents, stack_sorted
from merge_batch_util import (BATCH_READY, EXIT_CONFLICT, NEEDS_UPDATE, REMOTE, MergeBatchError, add_extra_paths,
                              add_label, check_name, drop_refs, drop_state, fetch_pr, git, items_in, load_state,
                              orphan_names, parse_prs, repo_info, save_state, short, state_dir, state_path,
                              worktrees)


def default_worktree(name):
    """<main checkout>-worktrees/batch-NAME, next to the main checkout (not nested in a lane)."""
    common = state_dir().parent  # the git common dir
    main = common.parent if common.name == ".git" else common
    return main.parent / f"{main.name}-worktrees" / f"batch-{name}"


def attrs_args(st):
    """git -c args that merge the --union paths line by line (an append-only ledger such as
    CHANGELOG.md conflicts in every pair of PRs that each add a line at the same spot)."""
    if not st.get("union"):
        return []
    path = state_dir() / f"{st['name']}.attrs"
    path.write_text("".join(f"{glob} merge=union\n" for glob in st["union"]), encoding="utf-8")
    return ["-c", f"core.attributesFile={path}"]


def merge_commit(wt, head):
    """The first-parent merge of ``head`` on the batch branch, or None."""
    for line in git("rev-list", "--first-parent", "--parents", "-n", "200", "HEAD", cwd=wt).stdout.splitlines():
        sha, *parents = line.split()
        if len(parents) > 1 and parents[1] == head:
            return sha
    return None


def union_warnings(st, merge, head):
    """One message per union-merged path (``merge=union``: --union or the repo's .gitattributes) in which the PR
    changed or removed lines that existed before it. A union merge keeps both sides of a hunk, so the old text
    of a reworded entry can stay beside the new one (CHANGELOG.md is the usual case): the seat checks it."""
    wt = st["worktree"]
    fork = git("merge-base", f"{merge}^1", head, cwd=wt, ok=(0, 1))
    if fork.returncode != 0:
        return []
    removed = {}
    for entry in git("diff", "--numstat", "-z", "--no-renames", fork.stdout.strip(), head, cwd=wt).stdout.split("\0"):
        _, _, rest = entry.partition("\t")
        gone, _, path = rest.partition("\t")
        if path and gone.isdigit() and int(gone) > 0:
            removed[path] = int(gone)
    if not removed:
        return []
    attrs = git(*attrs_args(st), "check-attr", "-z", "merge", "--", *removed, cwd=wt).stdout.split("\0")
    union = {attrs[i] for i in range(0, len(attrs) - 2, 3) if attrs[i + 2] == "union"}
    return [f"{path}: {removed[path]} pre-existing line(s) changed or removed in a path merged line by line "
            "(union); the old text can stay beside the new one, check it" for path in removed if path in union]


def record_merge(st, num, merge, resolved, files, recovered=False):
    """Add the merged record of PR ``num`` (merge commit ``merge``): Plan-Item values, union warnings. A merge
    found after a crash is listed with the resolution commits, because how it was made is unknown."""
    wt, head = st["worktree"], st["prs"][str(num)]["head"]
    msgs = git("log", "--format=%B%x00", f"{merge}^1..{merge}^2", cwd=wt).stdout
    warnings = union_warnings(st, merge, head)
    for text in warnings:
        print(f"warning: PR #{num}: {text}", file=sys.stderr)
    st["merged"].append({"pr": num, "head": head, "merge": merge, "resolved": resolved or recovered,
                         "recovered": recovered, "files": files, "items": items_in(msgs), "warnings": warnings})


def conflict_stop(st, number, files):
    print(f"CONFLICT PR #{number}: merge of {short(st['prs'][str(number)]['head'])} stopped in {st['worktree']}")
    for path in files:
        print(f"  {path}")
    print(f"resolve, git add, git commit (in {st['worktree']}), then: merge_batch.py continue --name {st['name']}")


def unmerged(wt):
    return [path for path in git("diff", "--name-only", "-z", "--diff-filter=U", cwd=wt).stdout.split("\0") if path]


def in_merge(wt):
    return git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=wt, ok=(0, 1)).returncode == 0


def step_done(st):
    """The first pending PR is settled (merged or skipped): drop its marker and move on."""
    st["merging"] = None
    st["pending"].pop(0)
    save_state(st)


def conflict(st, num, skip_conflicts):
    """A merge stopped on conflicts: stop for the agent (EXIT_CONFLICT) or, with --skip-conflicts, drop the PR."""
    wt, pr = st["worktree"], st["prs"][str(num)]
    files = unmerged(wt)
    if not skip_conflicts:
        st["pending"].pop(0)
        st.update(status="conflict", merging=None, current={"pr": num, "head": pr["head"], "files": files})
        save_state(st)
        conflict_stop(st, num, files)
        return EXIT_CONFLICT
    git("merge", "--abort", cwd=wt)
    labeled = add_label(st["repo"], num, NEEDS_UPDATE)
    if not labeled:
        print(f"warning: could not add the {NEEDS_UPDATE} label to PR #{num}", file=sys.stderr)
    st["skipped"].append({"pr": num, "head": pr["head"], "reason": "conflict", "files": files, "labeled": labeled})
    step_done(st)
    return None


def settle_merge(st, num, proc, skip_conflicts):
    """Record what ``git merge`` of PR ``num`` did: merged, a conflict, or a failure. EXIT_CONFLICT or None."""
    wt = st["worktree"]
    if proc.returncode == 0:
        record_merge(st, num, git("rev-parse", "HEAD", cwd=wt).stdout.strip(), False, [])
        step_done(st)
        return None
    if not in_merge(wt):  # git failed before it began a merge: the PR stays pending, continue retries it
        st["merging"] = None
        save_state(st)
        raise MergeBatchError(f"merge of PR #{num} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return conflict(st, num, skip_conflicts)


def recover_merging(st, skip_conflicts):
    """A run died after it saved the ``merging`` marker of a PR: take over what git did. A merge commit of that
    head is recorded (it is no "already in base" skip), an open conflict is picked up, nothing means the merge
    never ran. EXIT_CONFLICT or None."""
    mer, wt = st["merging"], st["worktree"]
    num = mer["pr"]
    if not st["pending"] or st["pending"][0] != num:  # not a state this tool writes: nothing to take over
        st["merging"] = None
        return None
    merge = merge_commit(wt, mer["head"])
    if merge:
        record_merge(st, num, merge, False, [], recovered=True)
        step_done(st)
        print(f"recovered the merge of PR #{num} ({short(merge)}) that an interrupted run made", file=sys.stderr)
        return None
    if in_merge(wt):
        if unmerged(wt):
            return conflict(st, num, skip_conflicts)
        git("merge", "--abort", cwd=wt)  # cut off before its commit and no conflict: undo it, merge again
    st["merging"] = None
    save_state(st)
    return None


def run_pending(st, skip_conflicts):
    """Merge the pending PRs in order; stop (exit 3) on a conflict unless skipping."""
    wt = st["worktree"]
    stop = recover_merging(st, skip_conflicts) if st.get("merging") else None
    while stop is None and st["pending"]:
        num = st["pending"][0]
        pr = st["prs"][str(num)]
        if git("merge-base", "--is-ancestor", pr["head"], "HEAD", cwd=wt, ok=(0, 1)).returncode == 0:
            st["skipped"].append({"pr": num, "head": pr["head"], "reason": "already in base", "files": []})
            step_done(st)
            continue
        st["merging"] = {"pr": num, "head": pr["head"]}
        save_state(st)  # before the merge: a crash right after it leaves this marker and continue finds the commit
        proc = git(*attrs_args(st), "merge", "--no-ff", "-m", f"Merge PR #{num}: {pr['title']}", pr["head"],
                   cwd=wt, ok=None)
        stop = settle_merge(st, num, proc, skip_conflicts)
    if stop is not None:
        return stop
    st.update(status="built", head=git("rev-parse", "HEAD", cwd=wt).stdout.strip())
    save_state(st)
    skipped = ", ".join(f"#{s['pr']} ({s['reason']})" for s in st["skipped"]) or "-"
    print(f"built {st['branch']} at {short(st['head'])} in {wt}: merged "
          f"{' '.join('#%d' % m['pr'] for m in st['merged']) or '-'}; skipped {skipped}")
    return 0


def stack_base(infos, order, parent, default):
    """The batch base is the repo's default branch, and every stack root must target it. A PR whose base is a
    branch that no PR of the batch owns (stacked on a PR outside the batch) would build against that branch,
    print MERGED and never reach the default branch."""
    outside = [infos[num] for num in order if num not in parent and infos[num]["base"] != default]
    if outside:
        raise MergeBatchError(f"every PR of a batch must target the default branch {default!r} or the PR it is "
                              "stacked on, in the batch: " + ", ".join(f"#{pr['number']} -> {pr['base']!r}"
                                                                        for pr in outside)
                              + " (stacked on a PR outside the batch? add that PR, or retarget this one)")
    return default


def fetch_heads(name, base, order, infos, repo):
    """Fetch origin/<base> and each PR head into refs/merge-batch/NAME/N; the heads must be what GitHub shows."""
    refs = [f"+refs/pull/{num}/head:refs/merge-batch/{name}/{num}" for num in order]
    git("fetch", "--quiet", REMOTE, f"+refs/heads/{base}:refs/remotes/{REMOTE}/{base}", *refs)
    for num in order:
        got = git("rev-parse", f"refs/merge-batch/{name}/{num}").stdout.strip()
        if got != infos[num]["head"]:
            drop_refs(name)
            raise MergeBatchError(f"PR #{num} is {short(got)} in {REMOTE} but {short(infos[num]['head'])} on GitHub: "
                                  f"run build again (is {REMOTE} the remote of {repo}?)")
    return {num: infos[num]["head"] for num in order}


def rule_problems(args, base, prs, heads):
    """Apply the plan rules (red CI, needs-update, HIGH apart from standard PRs, the caps) to the PRs of this
    build. They refuse it unless --force-set; then they are returned, to be logged in the state and the batch PR."""
    add_extra_paths(base, prs, heads)  # the old paths of renames count for the HIGH tier
    problems = set_problems(plan_batches(prs, base, args.max_prs, args.max_lines, high_patterns(args.high_path)))
    if problems and not args.force_set:
        raise MergeBatchError("the PRs break the plan rules: " + "; ".join(problems) + " (merge_batch.py plan "
                              "shows the batches; --force-set builds them anyway and logs it)")
    if problems:
        print("FORCE-SET: building despite the plan rules: " + "; ".join(problems), file=sys.stderr)
    return problems


def cmd_build(args):
    name = check_name(args.name)
    if state_path(name).exists():
        raise MergeBatchError(f"batch {name!r} is already in progress ({state_path(name)}): "
                              "continue, push or land it, or abort it")
    branch = f"batch/{name}"
    path = Path(args.worktree) if args.worktree else default_worktree(name)
    if git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", ok=(0, 1)).returncode == 0:
        raise MergeBatchError(f"branch {branch} already exists: pick another name or delete the branch")
    if path.exists():
        raise MergeBatchError(f"worktree path {path} already exists")
    repo, default = repo_info(args.repo)
    if not default:
        raise MergeBatchError(f"{repo} has no default branch")
    infos = {num: fetch_pr(repo, num) for num in parse_prs(args.prs)}
    for pr in infos.values():
        if pr["state"] != "OPEN":
            raise MergeBatchError(f"PR #{pr['number']} is {pr['state'].lower()}")
        if pr["draft"] and BATCH_READY not in pr["labels"]:
            raise MergeBatchError(f"PR #{pr['number']} is a draft without the {BATCH_READY} label")
    parent = stack_parents(list(infos.values()))
    order = stack_sorted(list(infos), parent)
    base = stack_base(infos, order, parent, default)
    heads = fetch_heads(name, base, order, infos, repo)
    try:
        forced = rule_problems(args, base, [infos[num] for num in order], heads)
    except MergeBatchError:
        drop_refs(name)
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    st = {"version": 1, "name": name, "repo": repo, "base": base, "branch": branch, "worktree": str(path.resolve()),
          "status": "creating", "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
          "skip_conflicts": bool(args.skip_conflicts), "union": list(args.union), "pending": order,
          "current": None, "merging": None, "merged": [], "skipped": [], "forced": forced, "head": None, "pr": None,
          "url": None, "prs": {str(n): {k: infos[n][k] for k in ("title", "head", "ref", "base")} for n in order}}
    save_state(st)  # before the worktree exists: a crash during `worktree add` leaves a state that abort cleans up
    try:
        git("worktree", "add", "--no-track", "-b", branch, str(path), f"{REMOTE}/{base}")
    except MergeBatchError:
        drop_state(st)
        raise
    st["status"] = "building"
    save_state(st)
    return run_pending(st, args.skip_conflicts)


def finish_current(st):
    """After a conflict stop: the agent's resolution must be a committed merge of that PR head."""
    cur, wt = st["current"], st["worktree"]
    num = cur["pr"]
    if in_merge(wt):
        raise MergeBatchError(f"the merge of PR #{num} is still in progress in {wt}: resolve the "
                              "conflicts, git add, git commit, then run continue again")
    if git("status", "--porcelain", "--untracked-files=no", cwd=wt).stdout.strip():
        raise MergeBatchError(f"{wt} has uncommitted changes: commit the resolution of PR #{num} first")
    merge = merge_commit(wt, cur["head"])
    if merge is None:
        raise MergeBatchError(f"{st['branch']} has no merge commit of PR #{num} ({short(cur['head'])}): the "
                              "resolution must be that merge (git merge --no-ff <head>, resolve, git commit)")
    record_merge(st, num, merge, True, cur["files"])
    st["current"] = None


def cmd_continue(args):
    st = load_state(args.name)
    if st["status"] == "conflict":
        finish_current(st)
    elif st["status"] == "creating":
        raise MergeBatchError(f"batch {st['name']!r} died while its worktree was made: abort it "
                              f"(merge_batch.py abort --name {st['name']}) and build again")
    elif st["status"] != "building":
        raise MergeBatchError(f"batch {st['name']!r} is {st['status']}: nothing to continue")
    st["status"] = "building"
    return run_pending(st, args.skip_conflicts or st.get("skip_conflicts", False))


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
    os.chdir(state_dir().parent)  # the git dir: the cwd may be the worktree that is about to go
    registered = dict(worktrees())
    paths = {path for path, branch in registered.items() if branch == st["branch"]}
    if st.get("worktree") in registered:
        paths.add(st["worktree"])
    for wt in sorted(paths):
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
