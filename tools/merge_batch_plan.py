"""Planning for tools/merge_batch.py: stack order, HIGH tier, batch caps, the plan table and Windows checks."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from merge_batch_util import (BATCH_READY, NEEDS_UPDATE, MergeBatchError, first_line, gh_json, git,
                              keeper_landing_configured, pr_labels, save_state)

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
    always when CI runs the full suite: a train or batch candidate (ci_select.full_run_required)."""
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


def plangraph_binary():
    """The plangraph binary the tool calls: PLANGRAPH_BIN, else the one on PATH (None when there is none)."""
    return os.environ.get("PLANGRAPH_BIN") or shutil.which("plangraph")


def keeper_cli(*args):
    """Run ``python -m plangraph.keeper.cli ARGS`` against this checkout's sources; returns the completed process."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[1] / "src"),
                                                                      os.environ.get("PYTHONPATH")])))
    return subprocess.run([sys.executable, "-m", "plangraph.keeper.cli", *args], capture_output=True, text=True,
                          timeout=120, check=False, env=env)


def declared_hosts(active):
    """``{item key: [hosts]}`` from the ``Deploy-Hosts:`` lines of the keeper items the active parts carry.

    Without a keeper, or for an item it does not know, nothing is declared and the commits' ``Deploy:`` trailers
    name the hosts."""
    if not keeper_landing_configured():
        return {}
    from plangraph.keeper.evidence import deploy_hosts

    declared = {}
    for key in sorted({key for rec in active["merged"] for key in rec.get("items", [])}):
        proc = keeper_cli("show", key, "--json")
        if proc.returncode:
            continue
        try:
            hosts = deploy_hosts(json.loads(proc.stdout))
        except ValueError:
            continue
        if hosts:
            declared[key] = hosts
    return declared


def export_units(wt, head, into):
    """Write ``deploy/systemd`` of the commit ``head`` below ``into``: the unit sources of the reported head,
    not the files that happen to be in the worktree."""
    import io
    import tarfile

    archive = subprocess.run(["git", "archive", "--format=tar", head, "deploy/systemd"], cwd=wt,
                             capture_output=True, check=False)
    if archive.returncode:
        raise MergeBatchError("deploy-plan failed: cannot read deploy/systemd of the batch head")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        for member in tar.getmembers():
            if member.isfile() and member.name.startswith("deploy/systemd/") and ".." not in member.name:
                target = Path(into) / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
    return Path(into) / "deploy" / "systemd"


def deploy_report(st, active):
    """The deploy part of the GO-REQ: ``plangraph land-train deploy-plan`` over the train parts, with the host
    install dry run as the probe. A refused target is listed with its blocker item key and its MISSING lines; the
    blocker items are filed with ``pg define --file`` when the keeper is configured. The unit sources are those
    of the reported head, and the hosts are the items' ``Deploy-Hosts:`` declarations before the commit trailers."""
    registry = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "plangraph" / "hosts.json"
    if not registry.is_file():
        return "deploy: unchecked (no hosts registry)"
    binary, wt = plangraph_binary(), st["worktree"]
    if not binary:
        raise MergeBatchError("deploy-plan failed: the plangraph binary is not on PATH (set PLANGRAPH_BIN)")
    spans = [{"pr": rec["pr"], "from": git("rev-parse", f"{rec['merge']}^1", cwd=wt).stdout.strip(),
              "head": rec["head"]} for rec in active["merged"]]
    with tempfile.TemporaryDirectory() as tmp:
        units = export_units(wt, active["head"], tmp)
        if any(re.search(r"\s", str(value)) for value in (binary, registry, wt, units)):
            raise MergeBatchError("deploy-plan failed: the binary, registry, worktree and temp paths must not hold spaces")
        spans_file, blockers, declared = Path(tmp) / "spans.json", Path(tmp) / "blockers.json", Path(tmp) / "declared.json"
        spans_file.write_text(json.dumps(spans))
        declared.write_text(json.dumps(declared_hosts(active)))
        probe = f"{binary} host-sync --install-dry-run --registry {registry} --repo {wt} --units {units}"
        argv = [binary, "land-train", "deploy-plan", "--repo", wt, "--spans", str(spans_file), "--registry",
                str(registry), "--probe-cmd", probe, "--declared", str(declared), "--blockers-out", str(blockers)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MergeBatchError(f"deploy-plan failed: {exc}") from None
        if proc.returncode:
            raise MergeBatchError(f"deploy-plan failed: {(proc.stderr.strip().splitlines() or ['no output'])[-1]}")
        if blockers.is_file() and json.loads(blockers.read_text()).get("items"):
            file_deploy_blockers(blockers)
    segments, missing = [], []
    for line in proc.stdout.splitlines():
        if line.startswith(("MISSING ", "REFUSED ")) and segments:
            missing.append(line)
            continue
        if missing:
            segments[-1] += f" ({', '.join(missing)})"
            missing = []
        segments.append(line)
    if missing:
        segments[-1] += f" ({', '.join(missing)})"
    return "; ".join(segments)


def file_deploy_blockers(path):
    """File the blocker items of refused deploy targets with ``pg define --file``, one item at a time so that a
    blocker the keeper already holds (a retry of the same GO-REQ) never rejects a new one. A failure is reported,
    not fatal."""
    if not keeper_landing_configured():
        print("deploy blocker items not filed: the keeper is not configured", file=sys.stderr)
        return
    items = json.loads(Path(path).read_text()).get("items", [])
    with tempfile.TemporaryDirectory() as tmp:
        for index, item in enumerate(items):
            one = Path(tmp) / f"item-{index}.json"
            one.write_text(json.dumps({"items": [item]}))
            proc = keeper_cli("define", "--file", str(one))
            reason = (proc.stderr.strip().splitlines() or ["no output"])[-1]
            if proc.returncode and "already exists" not in proc.stderr:
                print(f"deploy blocker item {item.get('key')} not filed: {reason}", file=sys.stderr)
