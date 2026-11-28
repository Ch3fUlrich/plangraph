"""Planning for tools/merge_batch.py: stack order, HIGH tier, batch caps, the plan table and Windows checks."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path

from ci_select import REPLAY_ENVIRONMENTS, REPLAY_RUNS, stable_observations
from merge_batch_util import (BATCH_READY, NEEDS_UPDATE, MergeBatchError, first_line, gh_json, git,
                              pr_labels, run, save_state, worktrees)
from preflight import run_test_process

try:
    from ci_commit_gate import AUTH_DOOR_PATHS, GATE_PATHS
except ImportError:  # merge_batch copied away from its sibling: high_patterns refuses
    AUTH_DOOR_PATHS, GATE_PATHS = (), ()

#: Always HIGH, even if the gate's own list is renamed.
KEEPER_PATHS = ("src/plangraph/keeper/*",)


def high_patterns(extra=()):
    """The AUTH_DOOR + gate globs of tools/ci_commit_gate.py, the keeper tree and --high-path.

    Fails closed: without the gate's own lists the HIGH set is unknown, and --high-path (which only adds
    globs) cannot stand in for them, so every command that tiers PRs refuses."""
    if not AUTH_DOOR_PATHS:
        raise MergeBatchError("tools/ci_commit_gate.py is not importable (or lists no AUTH_DOOR paths), so "
                              "the HIGH path list is unknown: keep it next to merge_batch.py")
    return tuple(dict.fromkeys((*AUTH_DOOR_PATHS, *GATE_PATHS, *KEEPER_PATHS, *extra)))


def high_hits(pr, patterns):
    """Match the full Git file list, including the old paths of renames."""
    paths = [*pr["files"], *pr.get("extra", ())]
    return [path for path in paths if any(fnmatch.fnmatchcase(path, pat) for pat in patterns)]


def stack_parents(prs):
    """number -> number of the same-repo PR whose head branch is this PR's base branch."""
    by_ref = {}
    for pr in sorted(prs, key=lambda p: p["number"]):
        if not pr["cross"]:
            by_ref.setdefault(pr["ref"], pr["number"])
    return {pr["number"]: by_ref[pr["base"]] for pr in prs
            if pr["base"] in by_ref and by_ref[pr["base"]] != pr["number"]}


def ancestors(number, parent):
    """(stack parents, nearest first; True when the chain loops back on itself)."""
    seen, chain = {number}, []
    while number in parent:
        number = parent[number]
        if number in seen:
            return chain, True
        seen.add(number)
        chain.append(number)
    return chain, False


def stack_sorted(numbers, parent):
    """Keep the given order, but every PR after the PR it is stacked on."""
    wanted, out = set(numbers), []

    def emit(num, trail=()):
        if num in out:
            return
        if num in trail:
            raise MergeBatchError(f"stack cycle through PR #{num}")
        if parent.get(num) in wanted:
            emit(parent[num], (*trail, num))
        out.append(num)

    for num in numbers:
        emit(num)
    return out


def exclusions(prs, base, parent):
    """number -> why the PR cannot join a batch (own state first, then its stack)."""
    by_num = {pr["number"]: pr for pr in prs}
    why = {}
    for pr in prs:
        if pr["state"] != "OPEN":
            why[pr["number"]] = f"not open ({pr['state'].lower()})"
        elif pr["draft"] and BATCH_READY not in pr["labels"]:  # the label overrides draft status
            why[pr["number"]] = "draft"
        elif pr["ci"] == "red":
            why[pr["number"]] = "ci red"
        elif NEEDS_UPDATE in pr["labels"]:
            why[pr["number"]] = f"label {NEEDS_UPDATE}"
    for pr in prs:
        num = pr["number"]
        chain, cycle = ancestors(num, parent)
        top = by_num[chain[-1]] if chain else pr
        if num in why:
            continue
        if cycle:
            why[num] = "stack cycle"
        elif top["base"] != base:
            why[num] = f"targets {top['base']!r}, not {base!r}"
        elif any(anc in why for anc in chain):
            bad = next(anc for anc in chain if anc in why)
            why[num] = f"stacked on #{bad} ({why[bad]})"
    return why


def plan_batches(prs, base, max_prs=6, max_lines=1200, patterns=()):
    """Order PRs (stacks together, base first), tier them and cut batches by the caps.

    HIGH and standard PRs fill separate batches, each under the same caps; a stack is never split.
    """
    by_num = {pr["number"]: pr for pr in prs}
    parent = stack_parents(prs)
    why = exclusions(prs, base, parent)
    units = {}
    for pr in prs:
        if pr["number"] not in why:
            chain, _ = ancestors(pr["number"], parent)
            units.setdefault(chain[-1] if chain else pr["number"], []).append(pr["number"])
    unit_list = []
    for members in units.values():
        members = stack_sorted(sorted(members), parent)
        unit_list.append({"prs": members, "lines": sum(by_num[n].get("batch_lines", by_num[n]["lines"]) for n in members),
                          "high": any(high_hits(by_num[n], patterns) for n in members)})
    unit_list.sort(key=lambda unit: min(unit["prs"]))
    batches, open_batch = [], {}  # open_batch: the batch of each tier that can still take a unit
    for unit in unit_list:
        tier = "HIGH" if unit["high"] else "STANDARD"  # HIGH and standard PRs never share a batch
        cur = open_batch.get(tier)
        if cur and (len(cur["prs"]) + len(unit["prs"]) > max_prs or cur["lines"] + unit["lines"] > max_lines):
            cur = None
        if cur is None:
            cur = open_batch[tier] = {"tier": tier, "prs": [], "lines": 0}
            batches.append(cur)
        cur["prs"] += unit["prs"]
        cur["lines"] += unit["lines"]
    batches.sort(key=lambda batch: min(batch["prs"]))
    for idx, batch in enumerate(batches, 1):
        batch.update(id=idx, base=base, files=len({f for n in batch["prs"] for f in by_num[n]["files"]}),
                     over_cap=len(batch["prs"]) > max_prs or batch["lines"] > max_lines)
    placed = {n: (batch["id"], batch["tier"]) for batch in batches for n in batch["prs"]}
    order = [n for batch in batches for n in batch["prs"]] + sorted(why)
    rows = [{"number": n, "title": by_num[n]["title"], "head": by_num[n]["head"], "ref": by_num[n]["ref"],
             "base": by_num[n]["base"], "draft": by_num[n]["draft"], "ci": by_num[n]["ci"],
             "files": len(by_num[n]["files"]), "lines": by_num[n]["lines"], "paths": by_num[n]["files"],
             "batch_lines": by_num[n].get("batch_lines", by_num[n]["lines"]),
             "generated_lines": by_num[n].get("generated_lines", 0),
             "generated_files": by_num[n].get("generated_files", []),
             "high": bool(high_hits(by_num[n], patterns)),
             "high_paths": high_hits(by_num[n], patterns)[:5], "batch": placed.get(n, (None, None))[0],
             "tier": placed.get(n, (None, None))[1], "after": parent.get(n), "reason": why.get(n)}
            for n in order]
    return {"base": base, "max_prs": max_prs, "max_lines": max_lines, "batches": batches, "prs": rows}


def set_problems(plan):
    """Why the PRs of ``plan`` (plan_batches over exactly the PRs of one build) are not a single plan batch:
    an excluded PR, HIGH and standard PRs together, a stack cut by the caps, or a batch over the caps."""
    why = [f"#{row['number']} {row['reason']}" for row in plan["prs"] if row["reason"]]
    if len(plan["batches"]) > 1:
        cut = ", ".join(f"{b['tier']} " + ",".join(f"#{n}" for n in b["prs"]) for b in plan["batches"])
        why.append(f"the plan cuts them into {len(plan['batches'])} batches ({cut}): HIGH and standard PRs are "
                   "reviewed apart and every batch stays under the caps")
    elif plan["batches"] and plan["batches"][0]["over_cap"]:
        batch = plan["batches"][0]
        why.append(f"{len(batch['prs'])} PR(s) and {batch['lines']} lines are over the caps "
                   f"(--max-prs {plan['max_prs']}, --max-lines {plan['max_lines']})")
    return why


def render_table(plan):
    head = ("B", "TIER", "PR", "HEAD", "CI", "DRAFT", "FILES", "LINES", "GENERATED", "BASE", "TITLE / NOTE")
    table = [head]
    for row in plan["prs"]:
        note = row["title"][:50]
        if row["reason"]:
            note = f"EXCLUDED: {row['reason']} | {note}"
        elif row["after"]:
            note = f"after #{row['after']} | {note}"
        if row["high_paths"]:
            note = f"HIGH via {row['high_paths'][0]} | {note}"
        table.append((str(row["batch"] or "-"), row["tier"] or "-", f"#{row['number']}", row["head"][:8],
                      row["ci"], "yes" if row["draft"] else "no", str(row["files"]), str(row["lines"]), str(row["generated_lines"]),
                      row["base"], note))
    widths = [max(len(line[i]) for line in table) for i in range(len(head))]
    out = ["  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in table]
    for batch in plan["batches"]:
        flag = "  OVER CAP" if batch["over_cap"] else ""
        out.append(f"batch {batch['id']} {batch['tier']}: --prs {','.join(map(str, batch['prs']))}  "
                   f"({len(batch['prs'])} PRs, {batch['files']} files, {batch['lines']} lines){flag}")
    if not plan["batches"]:
        out.append("no PR is eligible for a batch")
    return "\n".join(out)


WINDOWS_HOST_ENV = "PLANGRAPH_WINDOWS_HOST"
WINDOWS_TARGET_CAP_GB = 20
WINDOWS_TIMEOUT = 3600


def windows_commands(workflow_text):
    """Read cargo test commands from the CI Windows steps using YAML scalar semantics."""
    try:
        import yaml
    except ImportError as exc:
        raise MergeBatchError("reading the Windows job requires PyYAML from requirements-ci.txt") from exc
    try:
        steps = yaml.safe_load(workflow_text)["jobs"]["rust-windows"]["steps"]
    except yaml.YAMLError as exc:
        raise MergeBatchError(f"cannot parse tests.yml: {exc}") from exc
    except (KeyError, TypeError) as exc:
        raise MergeBatchError("tests.yml has no Windows job steps (rust-windows)") from exc

    commands = []
    try:
        for step in steps:
            value = step.get("run", "")
            commands.extend(shlex.split(line) for line in value.splitlines()
                            if line.lstrip().startswith("cargo test "))
    except (AttributeError, TypeError, ValueError) as exc:
        raise MergeBatchError(f"invalid run step in the Windows job of tests.yml: {exc}") from exc
    if not commands:
        raise MergeBatchError("the Windows job of tests.yml selects no cargo test command: refusing to run zero tests")
    return commands


def windows_needed(paths, full=False):
    """The Windows job runs whenever CI selects Rust, also for a Python-only change (tools/ci_select.py), and
    always when CI runs the full suite: the merge candidate into main (ci_select.full_run_required)."""
    import ci_select
    return bool(full or ci_select.select(list(paths))["rust"])


def windows_script(bundle, head, commands, cap_gb):
    """PowerShell for the disposable checkout: clone the bundle, test with its own capped target, always clean up."""
    steps = "\n".join(
        f"  Push-Location \"$root\\src\\rust\"; {' '.join(c)} --no-fail-fast 2>&1 | Write-Output; "
        "$code = $LASTEXITCODE; Pop-Location\n"
        f"  if ($code -ne 0) {{ $failed = $code }}\n"
        "  $size = (Get-ChildItem $env:CARGO_TARGET_DIR -Recurse -File -ErrorAction SilentlyContinue | "
        "Measure-Object Length -Sum).Sum / 1GB\n"
        f"  if ($size -gt {cap_gb}) {{ Write-Output 'target cap {cap_gb} GB exceeded'; $failed = 3; break }}"
        for c in commands)
    return (f"$ErrorActionPreference = 'Stop'\n$root = Join-Path $env:TEMP ('pg-train-' + [guid]::NewGuid().ToString('N').Substring(0,8))\n"
            f"$bundle = Join-Path $HOME '{bundle}'\n$failed = 0\n"
            "try {\n  New-Item -ItemType Directory -Path $root | Out-Null\n"
            "  git clone -q --no-checkout $bundle \"$root\\src\"\n"
            f"  git -C \"$root\\src\" checkout -q {head}\n"
            "  $env:CARGO_TARGET_DIR = \"$root\\target\"\n  $ErrorActionPreference = 'Continue'\n"
            f"{steps}\n}} finally {{\n  Set-Location $HOME\n  Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue\n"
            f"  Remove-Item -Force $bundle -ErrorAction SilentlyContinue\n}}\nexit $failed\n")


def _transport(argv, input_text=None, timeout=None):
    return subprocess.run(argv, input=input_text, capture_output=True, text=True, errors="replace", timeout=timeout)


def windows_gate(st, workflow_text, transport=_transport):
    """Run the CI Windows selection against the batch head on the Windows host; return the result line.

    None when the train needs no Windows job or no host is configured; MergeBatchError, naming the failing test
    ids, when the run is red. The line and its duration are kept in the state for the GO-REQ."""
    wt, head = st["worktree"], git("rev-parse", "HEAD", cwd=st["worktree"]).stdout.strip()
    paths = git("diff", "--name-only", f"{st['base_head']}...{head}", cwd=wt).stdout.split()
    import ci_select
    full = ci_select.full_run_required("pull_request", st.get("base", "main"), st["branch"], pr_labels(st))
    if not windows_needed(paths, full):
        return None
    host = os.environ.get(WINDOWS_HOST_ENV, "").strip()
    if not host:
        print(f"merge_batch: Windows tests not run: set {WINDOWS_HOST_ENV} to the native Windows ssh host", file=sys.stderr)
        return None
    commands = windows_commands(workflow_text)
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        bundle_name = f"train-{head[:12]}.bundle"
        bundle = Path(tmp, bundle_name)
        git("bundle", "create", str(bundle), f"refs/heads/{st['branch']}", cwd=wt)
        sent = transport(["scp", "-q", str(bundle), f"{host}:{bundle_name}"], None, WINDOWS_TIMEOUT)
        if sent.returncode:
            raise MergeBatchError(f"windows: cannot send the bundle to {host}: {first_line(sent.stderr)}")
        done = transport(["ssh", host, "powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
                         windows_script(bundle_name, head, commands, WINDOWS_TARGET_CAP_GB), WINDOWS_TIMEOUT)
    seconds = int(time.monotonic() - started)
    if done.returncode:
        import ci_select
        ids = ci_select._load_tool("preflight").failing_tests(done.stdout or "")
        detail = ", ".join(ids) if ids else first_line(done.stdout or done.stderr)
        raise MergeBatchError(f"windows tests failed on {host} for {head[:12]} after {seconds}s: {detail}")
    line = f"windows ok head={head[:12]} host={host} commands={len(commands)} seconds={seconds}"
    st["windows"] = {"head": head, "line": line}
    save_state(st)
    return line


def windows_report(st, head):
    """The Windows run of this exact head for the GO-REQ, or None when none ran for it."""
    recorded = st.get("windows") or {}
    return recorded["line"] if recorded.get("head") == head else None


RUNNER_PATHS = frozenset({
    "pytest.ini", "requirements-ci.txt", "pyproject.toml", "rust-toolchain.toml",
    "tools/affected_tests.py", "tools/ci_select.py", "tools/pgtest.py", "tools/preflight.py",
    "tools/dell_ci_jobs.py", "tools/dell_ci_run.py",
    "src/plangraph/spawner/proc_kill.py", "src/plangraph/spawner/winjob.py",
    "rust/worker-core/src/run/group.rs", "rust/worker-core/src/run/group_unix.rs",
    "rust/worker-core/src/run/group_windows.rs", "rust/worker-core/src/run/supervise.rs",
    "rust/worker-core/src/run/supervisor.rs", "rust/worker-core/src/stall.rs",
    "rust/worker-core/src/stall/deadline.rs", "rust/worker-core/src/stall/unix.rs",
    "rust/worker-core/src/stall/windows.rs",
})


def runner_wide_path(path):
    """Paths that can change the selection, execution or setup of unrelated tests."""
    parts = Path(path).parts
    if parts[-1] == "conftest.py":
        return True
    if path in RUNNER_PATHS or path.startswith((".github/workflows/", "tests/fixtures/", "rust/test_support/")):
        return True
    if parts[0] == "tests":
        return len(parts) == 2 and not parts[-1].startswith("test_")
    return False


def runner_wide_receipts(repo, infos, order, api=gh_json):
    """Require a green full Tests run at the exact head of every runner-wide part."""
    from merge_batch_land import full_job_problems
    from ci_select import CI_FULL, ci_full_description

    endpoint = f"repos/{repo}/actions"
    receipts = {}
    for num in order:
        pr = infos[num]
        if not any(runner_wide_path(path) for path in pr["own_paths"]):
            continue
        head = pr["head"]
        runs = api("api", f"{endpoint}/workflows/tests.yml/runs?head_sha={head}&per_page=100")["workflow_runs"]
        pr_evidence = None
        for row in sorted(runs, key=lambda row: row["id"], reverse=True):
            if (row.get("head_sha") != head or row.get("head_branch") != pr["ref"]
                    or (row.get("status"), row.get("conclusion")) != ("completed", "success")):
                continue
            run_id, attempt = row["id"], row.get("run_attempt", 1)
            if row.get("event") == "pull_request":
                if not any(p.get("number") == num for p in row.get("pull_requests") or []):
                    continue
                if pr_evidence is None:
                    tree = git("rev-parse", f"{head}^{{tree}}").stdout.strip()
                    statuses = api("api", f"repos/{repo}/commits/{head}/statuses?per_page=100")
                    pr_evidence = {s.get("description") for s in statuses
                                   if (s.get("context"), s.get("state")) == (CI_FULL, "success")}
                if ci_full_description(run_id, attempt, tree, tree) not in pr_evidence:
                    continue
            elif row.get("event") != "workflow_dispatch":
                continue
            if not full_job_problems(workflow_attempt_jobs(endpoint, run_id, attempt, api)):
                receipts[num] = run_id
                break
        else:
            raise MergeBatchError(f"PR #{num}: runner-wide part {head} needs green full CI "
                                  "on its own head before train entry")
    return receipts


def _draft_tests_jobs(jobs):
    witness = "Draft lane without test proof"
    def done(row, conclusion):
        return (row.get("status"), row.get("conclusion")) == ("completed", conclusion)

    gates = [job for job in jobs if job.get("name") == "ci-gate"]
    if len(gates) != 1 or not done(gates[0], "success"):
        return False
    steps = gates[0].get("steps")
    return (isinstance(steps, list)
            and any(job.get("name") == "Select suites for this change" and done(job, "skipped") for job in jobs)
            and all(job.get("name") == "ci-gate" or done(job, "skipped") for job in jobs)
            and sum(step.get("name") == witness for step in steps) == 1
            and all(done(step, "success" if step.get("name") in
                         (witness, "Set up job", "Complete job") else "skipped") for step in steps))


def draft_tests_run(repo, run, api):
    run_id, title = run.get("id"), run.get("display_title")
    attempt = run.get("run_attempt")
    if (run.get("event") == "pull_request" and type(run_id) is int and run_id > 0
            and title == f"Draft lane check {run_id}" and type(attempt) is int and attempt > 0):
        endpoint = f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
        response = api("api", endpoint)
        jobs = response.get("jobs")
        if isinstance(jobs, list) and response.get("total_count") == len(jobs) and _draft_tests_jobs(jobs):
            return "draft lane check without test proof"
    raise MergeBatchError(f"run {run_id}: draft lane marker's job is unconfirmed")


def workflow_attempt_jobs(endpoint, run_id, attempt, api):
    jobs, page = [], 1
    while True:
        data = api("api", f"{endpoint}/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
        jobs.extend(data["jobs"])
        if len(jobs) >= data["total_count"]:
            return jobs
        if not data["jobs"]:
            raise MergeBatchError("full_ci: incomplete job pagination")
        page += 1


def same_repo_parts(st, landed):
    """(ref, head) of the landed parts whose pull request head is a branch of this repository, by the
    PR's own fields. A fork's part, or one whose identity cannot be read, is left out of every delete."""
    parts = []
    for rec in landed:
        ref = st["prs"][str(rec["pr"])]["ref"]
        if not st.get("repo"):
            continue
        try:
            view = gh_json("pr", "view", str(rec["pr"]), "--repo", st["repo"],
                           "--json", "isCrossRepository,headRefName")
        except MergeBatchError:
            continue
        if view.get("isCrossRepository") is False and view.get("headRefName") == ref:
            parts.append((ref, rec["head"]))
    return parts


def lane_cleanup_landed(repo, st, parts):
    """Run `plangraph lane-cleanup` for exactly the frozen (ref, head) pairs of `same_repo_parts`.
    Exit 3 (a delete was refused) raises, so a strict settlement resumes at its cleanup step; moved
    and absent refs are done or re-evaluated by the daily run, which also takes older rounds."""
    pairs = [(ref, sha) for ref, sha in parts if ref.startswith("lane/")]
    if not pairs:
        return
    state = os.environ.get("PG_LANE_STATE", str(Path.home() / ".local/state/plangraph/lane-cleanup"))
    argv = ["plangraph", "lane-cleanup", "--repo", repo, "--state", state, "--gh-repo", st["repo"],
            "--refs", ",".join(f"refs/heads/{ref}:{sha}" for ref, sha in pairs), "--apply"]
    for flag, name in (("--claims-reader", "PG_SWEEP_CLAIMS_READER"), ("--remote", "PG_LANE_DELETE_REMOTE"),
                       ("--gh-host", "PG_LANE_GH_HOST")):
        if os.environ.get(name):
            argv.extend([flag, os.environ[name]])
    proc = run(argv, ok=(0, 3))
    sys.stderr.write(proc.stdout + proc.stderr)
    if proc.returncode == 3:
        raise MergeBatchError("lane cleanup deferred: a delete was refused; settle again or wait for the daily run")


try:
    import fcntl
except ImportError:  # no flock on Windows: the append stays atomic, a release can race an append
    fcntl = None


@contextmanager
def _locked(path):
    """One writer at a time over the holds file (check plus rewrite is one step under the lock)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _hold_file():
    state = os.environ.get("PG_LANE_STATE", str(Path.home() / ".local/state/plangraph/lane-cleanup"))
    return Path(state) / "lane-holds.txt"


def hold_lane_refs(refs):
    """Publish a hold (`refs/heads/<ref>` and the time) for each lane ref before its train can merge:
    the daily cleanup keeps a held ref until settlement releases it after a recorded writer-stop
    or the hold expires (lane_guard.rs, 14 days), so no ref is eligible before writer-stop can use it."""
    rows = [f"refs/heads/{ref}\t{int(time.time())}\n" for ref in refs if ref and ref.startswith("lane/")]
    if rows:
        path = _hold_file()
        with _locked(path):
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, "".join(rows).encode())
                os.fsync(fd)
            finally:
                os.close(fd)


def release_lane_refs(refs):
    """Drop the holds of `refs` after settlement recorded the writer-stop and ran the lane cleanup."""
    path = _hold_file()
    drop = {f"refs/heads/{ref}" for ref in refs}
    if not drop or not path.is_file():
        return
    with _locked(path):
        keep = [row for row in path.read_text().splitlines() if row.split("\t")[0] not in drop]
        temp = path.with_suffix(".tmp")
        temp.write_text("".join(row + "\n" for row in keep))
        os.replace(temp, path)


def writers_stopped(verification):
    """True when the recorded host verification names a writer-stop report for every host and none of
    its writers was kept: only then may settlement delete the landed lane refs the writers push to."""
    return bool(verification) and all(
        isinstance(host, dict) and isinstance(host.get("writers"), list)
        and all(row.get("status") != "kept" for row in host["writers"]) for host in verification)


def cleanup_landed(st, landed, *, strict=False, writers=None):
    """Best effort cleanup with exact state SHAs; all sweep protections still apply.

    `writers` is the writer-stop evidence of settlement: True (every host stopped its writers);
    anything else (not established, or a landing without host verification) keeps the lane refs
    out of both delete paths, held until the evidence is recorded or the hold expires. Remote deletes go only to identity-checked refs;
    the worktree targets are local-only (no --delete-remote without a verified remote target)."""
    try:
        repo = git("rev-parse", "--show-toplevel").stdout.strip()
        trees = worktrees()
        targets = [(st["worktree"], st["branch"], st["head"])] if st["worktree"] and Path(st["worktree"]).is_dir() else []
        for rec in landed:
            branch = st["prs"][str(rec["pr"])]["ref"]
            for path, name in trees:
                if name == branch or (not name and git("rev-parse", "HEAD", cwd=path).stdout.strip() == rec["head"]):
                    targets.append((path, name, rec["head"]))
        pins = os.environ.get("PG_PINS", str(Path.home() / ".config/plangraph/pins.json"))
        verified = same_repo_parts(st, landed)  # a fork's ref never reaches a remote delete
        if not writers:  # no positive writer-stop evidence (also a landing without host verification)
            held = [ref for ref, _ in verified if ref.startswith("lane/")]
            verified = [pair for pair in verified if pair[0] not in held]
            if held:
                print(f"lane refs held until writer-stop is recorded: {', '.join(held)}", file=sys.stderr)
        argv = ["pg-worktree-sweep", "--repo", repo, "--pins", pins, "--apply", "--include-merged"]
        if verified:
            argv.append("--delete-remote")
        roots = dict.fromkeys(Path(path).parent for path, _, _ in targets)
        for key in ("PG_WORKTREES", "PG_CLAUDE_WORKTREES", "PG_RUNS", "PG_RUN_WORKTREES"):
            if os.environ.get(key):
                roots[Path(os.environ[key])] = None
        for root in roots or (repo,):
            argv.extend(["--root", str(root)])
        for path, branch, sha in targets:
            argv.extend(["--target", f"{path}:{'refs/heads/' + branch if branch else '-'}:{sha}"])
        for branch, head in verified:
            argv.extend(["--remote-target", f"{branch}:{head}"])
        result = run(argv)
        print(result.stdout, end="", file=sys.stderr)
        if writers:
            lane_cleanup_landed(repo, st, verified)
            release_lane_refs([ref for ref, _ in verified])
    except MergeBatchError as exc:
        if strict:
            raise
        print(f"cleanup sweep failed: {exc}", file=sys.stderr)

_PG_TESTS = {
    "test_keeper_pg_unit.py", "test_keeper_pg_parity.py", "test_keeper_pg_copy.py",
    "test_projector_ddl.py", "test_worker_claim.py", "test_claim_parity_dispatch.py",
}
_RUST_TEST = re.compile(
    r"^(?P<indent>[ \t]*)(?P<attrs>(?:#\[[^\n]+\][ \t]*\n(?P=indent))+)"
    r"(?:async\s+)?fn\s+(?P<name>\w+)\b[^;{]*\{(?:[^\n]*\}[^\n]*|[^\n]*\n"
    r"(?:[^\n]*\n)*?(?P=indent)\})", re.M,
)


def _diff_entries(root, fork, head):
    """NUL-separated Git names, keeping rename source and destination together."""
    fields = iter(git("diff", "-M", "--name-status", "-z", fork, head, cwd=root).stdout.split("\0"))
    for status in filter(None, fields):
        source = next(fields)
        yield status[0], source, next(fields) if status.startswith(("C", "R")) else source


def _rust_changed_tests(root, fork, head, kind, source, path):
    text = git("show", f"{head}:{path}", cwd=root).stdout
    if kind == "A" or not source.endswith(".rs"):
        touched = set(range(1, text.count("\n") + 2))
    else:
        patch = git("diff", "--no-color", "--no-ext-diff", "-U0", f"{fork}:{source}", f"{head}:{path}", cwd=root).stdout
        touched = set()
        for line, size in re.findall(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", patch, re.M):
            start, count = int(line), int(size or 1)
            touched.update(range(start if count else max(1, start), start + (count or 2)))
    for test in _RUST_TEST.finditer(text):
        attrs = test["attrs"]
        if not re.search(r"(?m)^ *#\[(?:\w+::)?test(?:\(|\])", attrs):
            continue
        start = text.count("\n", 0, test.start()) + 1
        end = text.count("\n", 0, test.end()) + 1
        if not any(line in touched for line in range(start, end + 1)):
            continue
        # Leading inner cfgs apply to all tests in a file; outer cfgs apply to the function.
        inner = re.findall(r"^#!\[cfg\((.+)\)\]", text, re.M)
        platform = set(re.findall(r"#\[cfg\((.+)\)\]", attrs) + inner)
        windows_only = any(re.fullmatch(r'windows|not\(\s*unix\s*\)|target_(?:os|family)\s*=\s*"windows"', cfg)
                           for cfg in platform)
        unix_only = any(re.fullmatch(r'unix|not\(\s*windows\s*\)|target_family\s*=\s*"unix"|target_os\s*=\s*"(?:linux|macos)"', cfg)
                        for cfg in platform)
        yield test["name"], not windows_only, not unix_only


def _postgres_test(root, head, path):
    rel = Path(path).relative_to("tests")
    if rel.parts[0] == "contract":
        return True
    if len(rel.parts) > 1:
        return False
    if rel.name in _PG_TESTS:
        return True
    text = git("show", f"{head}:{path}", cwd=root).stdout
    return "sqlite3" not in text and bool(re.search(r"keeper_util|door_util|plangraph\.keeper|from plangraph import", text))


def _rust_replay_inventory(root, fork, head, paths, inventories):
    manifests = {name for name in git("ls-tree", "-r", "--name-only", "-z", head, "--", "rust", cwd=root).stdout.split("\0")
                 if name.endswith("/Cargo.toml")}
    packages, windows_packages = {}, None
    for kind, source, path in paths:
        manifest = next((f"{parent.as_posix()}/Cargo.toml" for parent in Path(path).parents
                         if f"{parent.as_posix()}/Cargo.toml" in manifests), None)
        if not manifest:
            continue
        for name, on_linux, on_windows in _rust_changed_tests(root, fork, head, kind, source, path):
            if on_linux:
                inventories["default"]["rust"].append([manifest, path, name])
            if not on_windows:
                continue
            if windows_packages is None:
                command_list = windows_commands(git("show", f"{head}:.github/workflows/tests.yml", cwd=root).stdout)
                windows_packages = {cmd[i + 1] for cmd in command_list for i, arg in enumerate(cmd[:-1])
                                    if arg in ("-p", "--package")}
            if manifest not in packages:
                packages[manifest] = tomllib.loads(git("show", f"{head}:{manifest}", cwd=root).stdout).get("package", {}).get("name")
            if packages[manifest] in windows_packages:
                inventories["windows"]["rust"].append([manifest, path, name])


def replay_inventory(root, base, head):
    """The applicable changed-test inventory for each CI environment, at the two commits."""
    fork = git("merge-base", base, head, cwd=root).stdout.strip()
    inventories = {env: {"python": [], "rust": []} for env in REPLAY_ENVIRONMENTS}
    rust_paths = []
    for kind, source, path in _diff_entries(root, fork, head):
        if kind not in ("A", "C", "M", "R"):
            continue
        if path.startswith("tests/") and re.fullmatch(r"test_.*\.py", Path(path).name):
            environment = "browser" if path.startswith("tests/browser/") else "default"
            inventories[environment]["python"].append(path)
            if environment == "default" and _postgres_test(root, head, path):
                inventories["postgres"]["python"].append(path)
        elif path.startswith("rust/") and path.endswith(".rs"):
            rust_paths.append((kind, source, path))
    if rust_paths:
        _rust_replay_inventory(root, fork, head, rust_paths, inventories)
    return {env: {lang: sorted(tests) for lang, tests in selected.items()}
            for env, selected in inventories.items() if any(selected.values())}


def _replay_dir(head):
    telemetry = Path(os.environ.get("PLANGRAPH_TELEMETRY_DIR") or Path.home() / ".local/share/plangraph/telemetry")
    return telemetry / "entry-replay" / head


def _read_replay(head, environment):
    try:
        return json.loads((_replay_dir(head) / f"{environment}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def replay_facts(root, base, head):
    """Read receipts for the part's exact own-base and head inventory."""
    inventory = replay_inventory(root, base, head)
    return {"replay_base": base, "replay_head": head, "replay_inventory": inventory,
            "replay_receipts": {env: _read_replay(head, env) for env in inventory}}


_PYTEST_STATUS = {"passed": "pass", "failed": "fail", "skipped": "skip"}
_RUST_STATUS = {"ok": "pass", "FAILED": "fail", "ignored": "skip"}


def _run_observation(command, cwd, env, evidence, label):
    """Write both streams even if a subprocess cannot start or hits its deadline."""
    logs = {stream: f"{evidence.name}/{label}.{stream}.log" for stream in ("stdout", "stderr")}
    out, err = (evidence.parent / path for path in logs.values())
    with out.open("wb") as stdout, err.open("wb") as stderr:
        try:
            result = run_test_process(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr, timeout=1800)
            rc = result.returncode
        except (subprocess.TimeoutExpired, OSError) as exc:
            stderr.write(f"\n{exc}\n".encode("utf-8", errors="replace"))
            rc = None
    return out.read_text(encoding="utf-8", errors="replace"), dict(command=command, label=label, returncode=rc, **logs)


def _observe_python(checkout, env, evidence, files, browser, observations, attempts, errors):
    from affected_tests import OUTCOME_PLUGIN, read_record_file
    options = ["-p", "no:cacheprovider", "--color=no"]
    if browser:
        options += ["-m", "browser"]
    output, collection = _run_observation([sys.executable, "-m", "pytest", *options, "--collect-only", "-q", *files],
                                          checkout, env, evidence, "python-collect")
    prefixes = tuple(f"{path}::" for path in files)
    names = {line.strip() for line in output.splitlines() if line.startswith(prefixes)}
    if collection["returncode"] != 0 or not all(any(name.startswith(p) for name in names) for p in prefixes):
        errors.append(f"could not collect all changed Python test files (logs {collection['stdout']} and {collection['stderr']})")
        return collection
    observations.update((name, []) for name in sorted(names))
    replay = OUTCOME_PLUGIN + "\nimport pytest, sys\nsys.exit(pytest.main(sys.argv[1:], plugins=[sys.modules[__name__]]))"
    for n in range(1, REPLAY_RUNS + 1):
        record_path = evidence / f"python-{n}.outcomes.json"
        argv = [sys.executable, "-c", replay, *options, "-v", "-rA", "--capture=tee-sys", *files]
        output, row = _run_observation(argv, checkout, dict(env, AFFECTED_OUTCOMES=str(record_path)), evidence, f"python-{n}")
        attempts.append(row | {"outcomes": record_path.relative_to(evidence.parent).as_posix()})
        recorded, _ = read_record_file(record_path)
        states = {name: _PYTEST_STATUS.get(recorded.get(name), "missing") for name in names}
        for description in re.findall(r"^XPASS (.+)$", output, re.M):
            # The recorder counts a non-strict XPASS as passed. The summary names it separately.
            candidates = (name for name in names if description == name or description.startswith(f"{name} "))
            if name := max(candidates, key=len, default=None):
                states[name] = "fail"
        for name, state in states.items():
            observations[name].append(state)
        if row["returncode"] not in (0, 1) or (row["returncode"] == 1 and "fail" not in states.values()):
            errors.append(f"Python replay {n} exited {row['returncode']} (logs {row['stdout']} and {row['stderr']})")
    return collection


def _observe_rust(checkout, env, evidence, tests, observations, attempts, errors):
    for test_index, (manifest, path, name) in enumerate(tests):
        states = observations[f"{path}::{name}"] = []
        relative = Path(path).relative_to(Path(manifest).parent)
        target = ["--test", relative.stem] if relative.parent == Path("tests") else []
        for n in range(1, REPLAY_RUNS + 1):
            command = ["cargo", "test", "--color", "never", "--manifest-path", manifest, *target, name, "--", "--test-threads=1"]
            output, row = _run_observation(command, checkout, env, evidence, f"rust-{test_index}-{n}")
            attempts.append(row)
            matches = re.findall(rf"^test (?:\S+::)?{re.escape(name)} \.\.\. (ok|FAILED|ignored)(?:,.*)?$", output, re.M)
            states.append(_RUST_STATUS[matches[0]] if len(matches) == 1 else "missing")
            if row["returncode"] not in (0, 101) or (row["returncode"] == 101 and states[-1] != "fail"):
                errors.append(f"Rust replay {name} #{n} exited {row['returncode']} (logs {row['stdout']} and {row['stderr']})")


def entry_replay(root, base, head, environment):
    """Replay the selected environment from the verified head and publish an atomic receipt."""
    if environment == "windows" and sys.platform != "win32":
        raise MergeBatchError("the windows replay needs a native Windows host")
    if environment != "windows" and not sys.platform.startswith("linux"):
        raise MergeBatchError(f"the {environment} replay needs a Linux host")
    if environment == "postgres" and not os.environ.get("PG_DSN"):
        raise MergeBatchError("the postgres replay needs PG_DSN for its isolated test database server")
    base, head = (git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=root).stdout.strip() for ref in (base, head))
    selected = replay_inventory(root, base, head).get(environment)
    if not selected:
        return None
    destination = _replay_dir(head).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix=f"{environment}-", dir=destination))
    start, started = time.monotonic(), time.time()
    attempts, observations, errors, collection = [], {}, [], None
    with tempfile.TemporaryDirectory(prefix="pg-entry-replay-") as scratch:
        checkout = Path(scratch) / "head"
        git("worktree", "add", "--detach", str(checkout), head, cwd=root)
        try:
            if git("rev-parse", "HEAD", cwd=checkout).stdout.strip() != head:
                raise MergeBatchError(f"the replay checkout is not {head}")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("PYTEST_ADDOPTS", "PYTEST_SHARD", "PG_KEEPER_BACKEND", "PG_DSN")
                   and not k.startswith("PYTEST_QUARANTINE_")}
            env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(checkout / "src"), env.get("PYTHONPATH"))))
            if environment == "postgres":
                env.update(PG_KEEPER_BACKEND="postgres", PG_DSN=os.environ["PG_DSN"])
            if selected["python"]:
                collection = _observe_python(checkout, env, evidence, selected["python"], environment == "browser",
                                             observations, attempts, errors)
            _observe_rust(checkout, env, evidence, selected["rust"], observations, attempts, errors)
            if git("status", "--porcelain", "--untracked-files=no", cwd=checkout).stdout:
                errors.append("tracked files changed during replay")
        finally:
            git("worktree", "remove", "--force", str(checkout), cwd=root)
    flaky = sorted(name for name, states in observations.items() if len(set(states) - {"missing"}) > 1)
    unsuccessful = sorted(name for name, states in observations.items() if not stable_observations(states))
    if unsuccessful:
        errors.append("unsuccessful observations: " + ", ".join(unsuccessful))
    result = {"base": base, "head": head, "environment": environment, "inventory": selected,
              "runs": REPLAY_RUNS, "ok": not errors, "observations": observations, "flaky": flaky,
              "errors": errors, "collection": collection, "attempts": attempts, "started": started,
              "elapsed_seconds": round(time.monotonic() - start, 3), "record": f"{evidence.name}/receipt.json"}
    raw = json.dumps(result, indent=2, sort_keys=True) + "\n"
    (evidence / "receipt.json").write_text(raw, encoding="utf-8")
    index = evidence / "latest.tmp"
    index.write_text(raw, encoding="utf-8")
    index.replace(destination / f"{environment}.json")
    return result | {"record": str(evidence / "receipt.json")}
