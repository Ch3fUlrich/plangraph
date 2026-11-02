"""build / continue / abort for tools/merge_batch.py: merge PR heads into batch/NAME with --no-ff."""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

from merge_batch_plan import stack_parents, stack_sorted
from merge_batch_util import (EXIT_CONFLICT, NEEDS_UPDATE, REMOTE, MergeBatchError, add_label, check_name, drop_state,
                              fetch_pr, git, items_in, load_state, parse_prs, repo_info, save_state, short,
                              state_dir, state_path)


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


def merged_record(wt, number, head, merge, resolved, files):
    msgs = git("log", "--format=%B%x00", f"{merge}^1..{merge}^2", cwd=wt).stdout
    return {"pr": number, "head": head, "merge": merge, "resolved": resolved, "files": files,
            "items": items_in(msgs)}


def conflict_stop(st, number, files):
    print(f"CONFLICT PR #{number}: merge of {short(st['prs'][str(number)]['head'])} stopped in {st['worktree']}")
    for path in files:
        print(f"  {path}")
    print(f"resolve, git add, git commit (in {st['worktree']}), then: merge_batch.py continue --name {st['name']}")


def run_pending(st, skip_conflicts):
    """Merge the pending PRs in order; stop (exit 3) on a conflict unless skipping."""
    wt = st["worktree"]
    while st["pending"]:
        num = st["pending"][0]
        pr = st["prs"][str(num)]
        if git("merge-base", "--is-ancestor", pr["head"], "HEAD", cwd=wt, ok=(0, 1)).returncode == 0:
            st["skipped"].append({"pr": num, "head": pr["head"], "reason": "already in base", "files": []})
        else:
            proc = git(*attrs_args(st), "merge", "--no-ff", "-m", f"Merge PR #{num}: {pr['title']}", pr["head"],
                       cwd=wt, ok=None)
            if proc.returncode == 0:
                head = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
                st["merged"].append(merged_record(wt, num, pr["head"], head, False, []))
            else:
                files = git("diff", "--name-only", "--diff-filter=U", cwd=wt).stdout.split()
                if git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=wt, ok=(0, 1)).returncode != 0:
                    save_state(st)  # PR stays pending: continue retries it
                    raise MergeBatchError(f"merge of PR #{num} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
                if not skip_conflicts:
                    st["pending"].pop(0)
                    st.update(status="conflict", current={"pr": num, "head": pr["head"], "files": files})
                    save_state(st)
                    conflict_stop(st, num, files)
                    return EXIT_CONFLICT
                git("merge", "--abort", cwd=wt)
                labeled = add_label(st["repo"], num, NEEDS_UPDATE)
                if not labeled:
                    print(f"warning: could not add the {NEEDS_UPDATE} label to PR #{num}", file=sys.stderr)
                st["skipped"].append({"pr": num, "head": pr["head"], "reason": "conflict", "files": files,
                                      "labeled": labeled})
        st["pending"].pop(0)
        save_state(st)
    st.update(status="built", head=git("rev-parse", "HEAD", cwd=wt).stdout.strip())
    save_state(st)
    skipped = ", ".join(f"#{s['pr']} ({s['reason']})" for s in st["skipped"]) or "-"
    print(f"built {st['branch']} at {short(st['head'])} in {wt}: merged "
          f"{' '.join('#%d' % m['pr'] for m in st['merged']) or '-'}; skipped {skipped}")
    return 0


def cmd_build(args):
    name = check_name(args.name)
    if state_path(name).exists():
        raise MergeBatchError(f"batch {name!r} is already in progress ({state_path(name)}): "
                              "continue, push or land it, or abort it")
    repo = args.repo or repo_info()[0]
    infos = {num: fetch_pr(repo, num) for num in parse_prs(args.prs)}
    for pr in infos.values():
        if pr["state"] != "OPEN" or pr["draft"]:
            raise MergeBatchError(f"PR #{pr['number']} is {'a draft' if pr['draft'] else pr['state'].lower()}")
    parent = stack_parents(list(infos.values()))
    order = stack_sorted(list(infos), parent)
    roots = {num: infos[num]["base"] for num in order if num not in parent}
    if len(set(roots.values())) != 1:
        raise MergeBatchError("one batch needs one base branch, the PRs target: " + ", ".join(
            f"#{n} -> {b}" for n, b in roots.items()) + " (a PR stacked on a PR outside the batch?)")
    base = next(iter(roots.values()))
    refs = [f"+refs/pull/{num}/head:refs/merge-batch/{name}/{num}" for num in order]
    git("fetch", "--quiet", REMOTE, f"+refs/heads/{base}:refs/remotes/{REMOTE}/{base}", *refs)
    for num in order:
        got = git("rev-parse", f"refs/merge-batch/{name}/{num}").stdout.strip()
        if got != infos[num]["head"]:
            raise MergeBatchError(f"PR #{num} is {short(got)} in {REMOTE} but {short(infos[num]['head'])} on GitHub: "
                                  f"run build again (is {REMOTE} the remote of {repo}?)")
    branch = f"batch/{name}"
    if git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", ok=(0, 1)).returncode == 0:
        raise MergeBatchError(f"branch {branch} already exists: pick another name or delete the branch")
    path = Path(args.worktree) if args.worktree else default_worktree(name)
    if path.exists():
        raise MergeBatchError(f"worktree path {path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "--no-track", "-b", branch, str(path), f"{REMOTE}/{base}")
    st = {"version": 1, "name": name, "repo": repo, "base": base, "branch": branch, "worktree": str(path.resolve()),
          "status": "building", "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
          "skip_conflicts": bool(args.skip_conflicts), "union": list(args.union), "pending": order,
          "current": None, "merged": [],
          "skipped": [], "head": None, "pr": None, "url": None,
          "prs": {str(n): {k: infos[n][k] for k in ("title", "head", "ref", "base")} for n in order}}
    save_state(st)
    return run_pending(st, args.skip_conflicts)


def finish_current(st):
    """After a conflict stop: the agent's resolution must be a committed merge of that PR head."""
    cur, wt = st["current"], st["worktree"]
    num = cur["pr"]
    if git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=wt, ok=(0, 1)).returncode == 0:
        raise MergeBatchError(f"the merge of PR #{num} is still in progress in {wt}: resolve the "
                              "conflicts, git add, git commit, then run continue again")
    if git("status", "--porcelain", "--untracked-files=no", cwd=wt).stdout.strip():
        raise MergeBatchError(f"{wt} has uncommitted changes: commit the resolution of PR #{num} first")
    merge = merge_commit(wt, cur["head"])
    if merge is None:
        raise MergeBatchError(f"{st['branch']} has no merge commit of PR #{num} ({short(cur['head'])}): the "
                              "resolution must be that merge (git merge --no-ff <head>, resolve, git commit)")
    st["merged"].append(merged_record(wt, num, cur["head"], merge, True, cur["files"]))
    st["current"] = None


def cmd_continue(args):
    st = load_state(args.name)
    if st["status"] == "conflict":
        finish_current(st)
    elif st["status"] != "building":
        raise MergeBatchError(f"batch {st['name']!r} is {st['status']}: nothing to continue")
    st["status"] = "building"
    return run_pending(st, args.skip_conflicts or st.get("skip_conflicts", False))


def cmd_abort(args):
    st = load_state(args.name)
    wt = Path(st["worktree"])
    if wt.exists():
        git("merge", "--abort", cwd=str(wt), ok=None)
        git("worktree", "remove", "--force", str(wt))
    git("branch", "-D", st["branch"], ok=None)
    drop_state(st)
    note = f"; batch PR #{st['pr']} is still open on the remote" if st.get("pr") else ""
    print(f"aborted batch {st['name']}: removed {wt} and {st['branch']}{note}")
    return 0
