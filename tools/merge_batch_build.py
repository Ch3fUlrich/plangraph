"""build / continue / abort for tools/merge_batch.py: merge PR heads into batch/NAME with --no-ff.

The state file is saved before every step that changes the repo (the worktree, each merge), so a crash
leaves a marker that continue and abort act on: status ``creating`` (the worktree may be half made) or a
``merging`` PR (its merge commit may exist already)."""
from __future__ import annotations

import datetime
import os
import shlex
import sys
from pathlib import Path

from merge_batch_plan import high_patterns, plan_batches, set_problems, stack_parents, stack_sorted
from merge_batch_util import (BATCH_READY, EXIT_CONFLICT, NEEDS_UPDATE, REMOTE, MergeBatchError, refresh_diff, run,
                              add_label, check_name, drop_refs, drop_state, fetch_pr, git, items_in, load_state,
                              orphan_names, parse_prs, first_line, repo_info, save_state, short, state_dir, state_path,
                              workflow_gate, worktrees, gh_json)


def merge_trailers(st):
    """The trailer paragraph of every merge commit the build makes: the batch plan key as ``Plan-Item`` (the
    keeper finds the batch item in git without a merge body) and ``Land-Base``, the base the batch is built and
    tested on (the landing mode tested-ff leases main on it, and it is part of the tested commit)."""
    return "\n".join([*([f"Plan-Item: {st['plan']}"] if st.get("plan") else []), f"Land-Base: {st['base_head']}"])


def default_worktree(name):
    """<main checkout>-worktrees/batch-NAME, next to the main checkout (not nested in a lane)."""
    common = state_dir().parent  # the git common dir
    main = common.parent if common.name == ".git" else common
    return main.parent / f"{main.name}-worktrees" / f"batch-{name}"


def default_union(base):
    """Root .gitattributes at the fetched base is the single ledger policy source.

    Preserve quoted patterns when copying them into the batch attributes file.
    Persist the result in state so crash recovery uses the same policy.
    """
    listed = git("ls-tree", "--name-only", base, "--", ".gitattributes").stdout
    if not listed.strip():
        return []
    patterns = []
    for line in git("show", f"{base}:.gitattributes").stdout.splitlines():
        if line.lstrip().startswith("#"):
            continue
        fields = shlex.split(line, comments=False, posix=False)
        if not fields or fields[0].startswith("[attr]"):
            continue
        merge = next((a for a in reversed(fields[1:])
                      if a in ("merge", "-merge", "!merge") or a.startswith("merge=")), None)
        if merge == "merge=union":
            patterns.append(fields[0])
    return patterns


def attrs_args(st):
    """git -c args for the saved .gitattributes defaults plus explicit --union paths (an append-only ledger such as
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
        git("merge", "--abort", cwd=wt)  # undo it and merge again; a failed abort raises here and keeps the marker
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
        proc = git(*attrs_args(st), "merge", "--no-ff", "-m", f"Merge PR #{num}: {pr['title']}",
                   "-m", merge_trailers(st), pr["head"], cwd=wt, ok=None)
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
    try:
        git("fetch", "--quiet", REMOTE, f"+refs/heads/{base}:refs/remotes/{REMOTE}/{base}", *refs)
        for num in order:
            got = git("rev-parse", f"refs/merge-batch/{name}/{num}").stdout.strip()
            if got != infos[num]["head"]:
                raise MergeBatchError(f"PR #{num} is {short(got)} in {REMOTE} but {short(infos[num]['head'])} on "
                                      f"GitHub: run build again (is {REMOTE} the remote of {repo}?)")
    except MergeBatchError:
        drop_refs(name)  # a refused build leaves no refs behind for status to call an orphan
        raise
    return {num: infos[num]["head"] for num in order}


def rule_problems(args, base, prs, heads):
    """Apply the plan rules (red CI, needs-update, HIGH apart from standard PRs, the caps) to the PRs of this
    build. They refuse it unless --force-set; then they are returned, to be logged in the state and the batch PR."""
    refresh_diff(base, prs, heads)  # use the same fetched diff metadata as plan
    problems = set_problems(plan_batches(prs, base, args.max_prs, args.max_lines, high_patterns(args.high_path)))
    if problems and not args.force_set:
        raise MergeBatchError("the PRs break the plan rules: " + "; ".join(problems) + " (merge_batch.py plan "
                              "shows the batches; --force-set builds them anyway and logs it)")
    if problems:
        print("FORCE-SET: building despite the plan rules: " + "; ".join(problems), file=sys.stderr)
    return problems


def policy_runner(argv):
    """Run pg-policy: PG_POLICY_BIN when set, else cargo run on this checkout. Returns (returncode, stdout, stderr)."""
    root = git("rev-parse", "--show-toplevel").stdout.strip()
    binary = os.environ.get("PG_POLICY_BIN")
    command = ([binary] if binary else ["cargo", "run", "--quiet", "--locked", "--manifest-path",
                                        str(Path(root) / "rust/Cargo.toml"), "-p", "pg-policy", "--"]) + argv
    proc = run(command, cwd=root, ok=None)
    return proc.returncode, proc.stdout, proc.stderr


def open_pr_heads(name, repo, infos):
    """Every open pull request of the repository outside the batch: {number: {head, ref, base}}, heads fetched.

    The part-entry predicate compares a part with all open heads, not only with the parts of this build."""
    rows = gh_json("pr", "list", "--repo", repo, "--state", "open", "--limit", "1000", "--json",
                   "number,headRefName,baseRefName,headRefOid")
    outside = [r for r in rows if r["number"] not in infos]
    for row in outside:
        number, head = row["number"], row["headRefOid"]
        fetch_error = None
        try:
            git("fetch", "--quiet", REMOTE, f"+refs/pull/{number}/head:refs/merge-batch/{name}/open/{number}")
        except MergeBatchError as exc:
            fetch_error = exc
        if git("cat-file", "-t", head, ok=None).stdout.strip() != "commit":
            detail = f": {first_line(str(fetch_error))}" if fetch_error else " after fetch"
            raise MergeBatchError(f"open PR #{number} advertised head {head} unavailable{detail}") from fetch_error
    return {r["number"]: {"head": r["headRefOid"], "ref": r["headRefName"], "base": r["baseRefName"]} for r in outside}


def part_entry_problems(base, order, heads, parent, runner=policy_runner, opens=None, refs=None):
    """Apply the part-entry predicate (imported_history, undeclared_dependency, shared_history) to every part.

    A part's parent is the pull request its base ref names (a part of the batch or another open one); the
    other heads are every open head outside its chain, batch parts and ``opens`` alike."""
    universe = {num: {"head": heads[num], "ref": (refs or {}).get(num), "base": None} for num in order}
    for num, row in (opens or {}).items():
        universe[num] = row
    by_ref = {row["ref"]: num for num, row in universe.items() if row.get("ref")}
    up_of = dict(parent)
    for num, row in universe.items():
        if num not in up_of and row.get("base") in by_ref and by_ref[row["base"]] != num:
            up_of[num] = by_ref[row["base"]]
    problems = []
    for num in order:
        chain, up = {num}, num
        while up in up_of and up_of[up] not in chain:
            up = up_of[up]
            chain.add(up)
        grew = True
        while grew:  # descendants: pull requests whose chain reaches num
            grew = False
            for child, mother in up_of.items():
                if mother in chain and child not in chain:
                    chain.add(child)
                    grew = True
        others = [row["head"] for other, row in universe.items() if other not in chain]
        code, out, err = runner(["--part-entry", f"{REMOTE}/{base}", heads[num],
                                 universe[up_of[num]]["head"] if num in up_of else "-", *others])
        if code:
            detail = "; ".join(line.strip() for line in (out or err).splitlines() if line.strip())
            problems.append(f"PR #{num}: {detail or 'part entry check failed'}")
    return problems


#: PR label recording the rule 3.46 flake disposition for a red attempt that a rerun turned green.
FLAKE_LABEL = "flake-dispositioned"


def tests_runs(repo, pr, head, api):
    """Tests runs explicitly associated with this pull request and commit."""
    runs = api("api", f"repos/{repo}/actions/workflows/tests.yml/runs?head_sha={head}&event=pull_request&per_page=100")
    return [r for r in runs.get("workflow_runs", []) if r.get("head_sha") == head
            and any(p.get("number") == pr for p in r.get("pull_requests") or [])]


def ci_attempts(repo, pr, head):
    """Read actual attempt conclusions, requiring a successful ci-gate for a green Tests run."""
    rows = []
    for run in sorted(tests_runs(repo, pr, head, gh_json), key=lambda r: r["id"]):
        run_id, last = run["id"], run.get("run_attempt", 1)
        for number in range(1, last + 1):
            endpoint = f"repos/{repo}/actions/runs/{run_id}/attempts/{number}"
            attempt = run if number == last else gh_json("api", endpoint)
            conclusion = attempt.get("conclusion") if attempt.get("status") == "completed" else None
            if conclusion == "success":
                jobs = gh_json("api", endpoint + "/jobs?per_page=100")["jobs"]
                if not any(job.get("name") == "ci-gate" and job.get("status") == "completed"
                           and job.get("conclusion") == "success" for job in jobs):
                    conclusion = "ci-gate missing or unsuccessful"
            rows.append({"run_id": run_id, "attempt": number, "conclusion": conclusion})
    return rows


def part_readiness_problems(repo, infos, order, attempts_for=ci_attempts):
    """The readiness verdict (tools/ci_select.py ready_problems) of every part, from its own CI on its head.

    ci-gate is green only when every selected job passed: the always-on checks, the entry suites and, when the
    part selects Rust, the Windows job; so its rollup stands for those three facts."""
    import ci_select
    problems = []
    for num in order:
        pr = infos[num]
        attempts = attempts_for(repo, num, pr["head"])
        done = sorted((a for a in attempts if a.get("conclusion")), key=ci_select.attempt_key)
        green = bool(done and done[-1]["conclusion"] == "success")
        rust = bool(ci_select.select(pr["files"])["rust"]) if pr["files"] and not pr["truncated"] else True
        dispositions = [ci_select.attempt_key(a) for a in done[:-1]] if FLAKE_LABEL in pr["labels"] else []
        found = ci_select.ready_problems({
            "fast_checks": green, "entry_set": green, "windows_required": rust, "windows": green if rust else None,
            "attempts": attempts, "flake_dispositions": dispositions})
        problems += [f"PR #{num}: {p}" for p in found]
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
        opens = open_pr_heads(name, repo, infos)
        entry = (part_entry_problems(base, order, heads, parent, opens=opens,
                                     refs={n: infos[n]["ref"] for n in order})
                 + part_readiness_problems(repo, infos, order))
        if entry and not args.force_set:
            raise MergeBatchError("part entry refused: " + "; ".join(entry) + " (--force-set builds it anyway and logs it)")
        if entry:
            print("FORCE-SET: building despite part entry: " + "; ".join(entry), file=sys.stderr)
        forced = [*forced, *entry]
        for num in order:  # a frozen batch push cannot be fixed by a history rewrite: refuse before the build
            workflow_gate(None, f"{REMOTE}/{base}", f"refs/merge-batch/{name}/{num}")
    except MergeBatchError:
        drop_refs(name)
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    st = {"version": 1, "name": name, "repo": repo, "base": base, "branch": branch, "worktree": str(path.resolve()),
          "base_head": git("rev-parse", f"{REMOTE}/{base}").stdout.strip(),
          "plan": args.plan, "deviations": args.deviations,
          "status": "creating", "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
          "skip_conflicts": bool(args.skip_conflicts), "union": list(dict.fromkeys(default_union(f"{REMOTE}/{base}") + list(args.union))), "pending": order,
          "current": None, "merging": None, "merged": [], "skipped": [], "forced": forced, "head": None, "pr": None,
          "url": None, "prs": {str(n): {k: infos[n][k] for k in ("title", "head", "ref", "base")} for n in order}}
    save_state(st)  # before the worktree exists: a crash during `worktree add` leaves a state that abort cleans up
    try:
        git("worktree", "add", "--no-track", "-b", branch, str(path), f"{REMOTE}/{base}")
    except MergeBatchError as exc:
        # the branch or the worktree may exist half made; the state stays (status "creating") so abort finds
        # them and removes only what this build made (it verified the branch and the path absent before)
        raise MergeBatchError(f"could not make the worktree of batch {name!r}: {exc} "
                              f"(left for abort: run merge_batch.py abort --name {name})") from None
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


def _jobs_of_part(repo, pr, head):
    """Jobs of the latest completed Tests run for this part PR and head."""
    runs = [r for r in tests_runs(repo, pr, head, gh_json) if r.get("status") == "completed"]
    if not runs:
        return []
    run = max(runs, key=lambda r: r["id"])
    jobs = gh_json("api", f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
    return [j for j in jobs if j.get("started_at") and j.get("completed_at") and j.get("created_at")]


def record_part_cost(st, run_id, head, telemetry_dir=None):
    """Append the CI queue and run cost to the part-cost telemetry: one row per merged part, from the run of its
    own head, and one for the batch candidate's run. Never fails the push."""
    import ci_select
    folder = Path(telemetry_dir or os.environ.get("PLANGRAPH_TELEMETRY_DIR")
                  or Path.home() / ".local/share/plangraph/telemetry")
    rows = []
    for rec in st.get("merged") or []:
        rows.append(("part", rec["pr"], rec["head"], lambda p=rec["pr"], h=rec["head"]: _jobs_of_part(st["repo"], p, h)))
    rows.append(("candidate", st.get("pr") or 0, head, lambda: gh_json(
        "api", f"repos/{st['repo']}/actions/runs/{run_id}/jobs?per_page=100")["jobs"]))
    written = []
    for kind, number, sha, jobs_of in rows:
        try:
            jobs = [j for j in jobs_of() if j.get("started_at") and j.get("completed_at") and j.get("created_at")]
            if jobs:
                record = dict(ci_select.part_cost(jobs, number, sha), kind=kind)
                ci_select.write_part_cost(folder / "part-cost.jsonl", record)
                written.append(record)
        except (MergeBatchError, KeyError, ValueError, TypeError, OSError) as exc:
            print(f"merge_batch: part cost of #{number} not recorded: {exc}", file=sys.stderr)
    return written
