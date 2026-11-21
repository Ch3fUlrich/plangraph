"""push / land / status for tools/merge_batch.py: the batch PR, its gate checks, the MERGED lines."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from merge_batch_build import tests_runs
from merge_batch_util import (NEEDS_UPDATE, REMOTE, MergeBatchError, add_label, all_states, drop_state, first_line, gh,
                              gh_json, git, load_state, orphan_names, save_state, short, update_pr, worktrees,
                              batch_workflow_gate, part_review, run)

#: gh pr checks buckets that satisfy a required check (a skipped required check passes on GitHub too).
GREEN = ("pass", "skipping")
# Job id -> display name of the tests workflow (tests/test_ci_job_names.py keeps it equal to
# tests.yml). The API reports the display name; the id still counts for runs of older heads.
JOB_DISPLAY = {
    "select": "Select suites for this change",
    "scrub": "Public scrub: no private names or secrets",
    "file-size": "File size limit (400 lines)",
    "new-code-lang": "New code is Rust",
    "changelog-docs": "Changelog fragment and docs",
    "browser": "UI tests in headless Chromium",
    "rust": "Rust workspace on Linux: fmt, clippy, tests",
    "rust-windows": "Rust spawner and worker core tests on Windows",
    "rust-worker": "Rust worker crate: fmt, clippy, tests",
    "rust-spike": "Rust replay spike tests",
    "ci-gate": "ci-gate",
}
# Sharded job id -> display name prefix; the shard suffix is " (shard N of M)", N from 1.
SHARD_DISPLAY = {
    "pytest": "Python tests, SQLite keeper",
    "pytest-postgres": "Python tests, Postgres keeper: contract and storage",
}


def job_named(seen, key: str) -> bool:
    return key in seen or JOB_DISPLAY[key] in seen


def shard_named(seen, suite: str, index: int) -> bool:
    """True when shard *index* (0-based) of *suite* ran, under the old or the new name."""
    if f"{suite} ({index})" in seen:
        return True
    pattern = re.compile(re.escape(SHARD_DISPLAY[suite]) + rf" \(shard {index + 1} of \d+\)")
    return any(pattern.fullmatch(name) for name in seen)
#: Every bucket gh prints; any other word is unknown, and unknown is never green.
BUCKETS = ("pass", "fail", "pending", "skipping", "cancel")
#: A verdict line that says fail, whatever precedes it (quote mark, bullet, bold, underscores): it refuses the approval.
BAD_VERDICT = re.compile(r"(?<![A-Za-z0-9])verdict[\W_]*:[\W_]*(?:fail|fix|halt|reject|change|request[\W_]*change)",
                         re.IGNORECASE)
HEAD_LINE = re.compile(r"HEAD: ([0-9a-fA-F]{40})")
PASS_LINES = ("VERDICT: PASS", "VERDICT: pass")


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def render_body(st):
    rows = ["| PR | Plan-Item | Head | Merge | Title |", "|---|---|---|---|---|"]
    for rec in st["merged"]:
        title = st["prs"][str(rec["pr"])]["title"]
        rows.append(f"| #{rec['pr']} | {cell(', '.join(rec['items']) or '-')} | `{short(rec['head'])}` | "
                    f"`{short(rec['merge'])}` | {cell(title)} |")
    body = [f"Merge batch `{st['name']}` into `{st['base']}`: {len(st['merged'])} PRs, merged with `--no-ff` "
            f"in this order.", "", *rows]
    if st["skipped"]:
        body += ["", "Skipped (not in this batch):"]
        body += [f"- #{s['pr']} (`{short(s['head'])}`): {s['reason']}"
                 + (f" in {', '.join(f'`{f}`' for f in s['files'])}" if s["files"] else "") for s in st["skipped"]]
    if st.get("forced"):
        body += ["", "Built with --force-set, past the plan rules: " + "; ".join(st["forced"])]
    resolved = [rec for rec in st["merged"] if rec["resolved"]]
    if resolved:
        body += ["", "Resolution commits (conflicts resolved by hand, review these hunks):"]
        body += [f"- `{short(rec['merge'])}` PR #{rec['pr']}: "
                 + (", ".join(f"`{f}`" for f in rec["files"]) or "merge found after an interrupted build, check it")
                 for rec in resolved]
    notes = [f"- PR #{rec['pr']}: {text}" for rec in st["merged"] for text in rec.get("warnings", [])]
    if notes:
        body += ["", "Check these (a PR changed old lines of a path that is merged line by line):", *notes]
    return "\n".join(body) + "\n"


def adopt_open_pr(st):
    """A push that died after `gh pr create` left an open batch PR the state does not know: take it over
    (same repo, same base), rather than failing on GitHub's "a pull request already exists"."""
    found = gh_json("pr", "list", "--repo", st["repo"], "--head", st["branch"], "--state", "open", "--json",
                    "number,url,baseRefName,isCrossRepository", "--limit", "5")
    for pr in found:
        if pr["baseRefName"] == st["base"] and not pr["isCrossRepository"]:
            st.update(pr=pr["number"], url=pr["url"])
            return


# --- Windows tests of the merged head before the push (LAND-TRAIN-WINDOWS-TESTS-BEFORE-PUSH) ------------------
#: Env: the ssh host with native Windows (ws-build); unset skips the gate and says so.
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


def pr_labels(st):
    """The labels CI sees on the batch pull request (none while it does not exist yet)."""
    if not st.get("pr"):
        return []
    try:
        view = gh_json("pr", "view", str(st["pr"]), "--repo", st["repo"], "--json", "labels")
        return [label["name"] for label in view.get("labels", [])]
    except (MergeBatchError, KeyError, TypeError):
        return []


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


def windows_report(st, head):
    """The Windows run of this exact head for the GO-REQ, or None when none ran for it."""
    recorded = st.get("windows") or {}
    return recorded["line"] if recorded.get("head") == head else None


def cmd_push(args):
    st = load_state(args.name)
    wt = st["worktree"]
    if st["status"] not in ("built", "pushed"):
        raise MergeBatchError(f"batch {st['name']!r} is {st['status']}: finish the build first")
    if not st["merged"]:
        raise MergeBatchError(f"batch {st['name']!r} merged no PR (all skipped): abort it")
    if git("status", "--porcelain", "--untracked-files=no", cwd=wt).stdout.strip():
        raise MergeBatchError(f"{wt} has uncommitted changes: commit them before push")
    head = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    if not st.get("pr"):
        adopt_open_pr(st)
    workflow = Path(wt, ".github/workflows/tests.yml")
    if workflow.is_file():   # a red Windows run refuses the push before anything leaves the machine
        windows_gate(st, workflow.read_text(encoding="utf-8"))
    git("push", REMOTE, f"refs/heads/{st['branch']}:refs/heads/{st['branch']}", cwd=wt)
    title = f"batch {st['name']}: " + " ".join(f"#{rec['pr']}" for rec in st["merged"])
    with tempfile.TemporaryDirectory() as tmp:
        body = Path(tmp, "body.md")
        body.write_text(render_body(st), encoding="utf-8")
        if st.get("pr"):
            if gh_json("pr", "view", str(st["pr"]), "--repo", st["repo"], "--json", "state")["state"] != "OPEN":
                raise MergeBatchError(f"batch PR #{st['pr']} is not open any more: abort and build again")
            update_pr(st["repo"], st["pr"], title, body)
        else:
            out = gh("pr", "create", "--repo", st["repo"], "--base", st["base"], "--head", st["branch"],
                     "--title", title, "--body-file", str(body)).stdout
            found = re.search(r"\S+/pull/(\d+)", out)  # gh may print warnings around the url
            if not found:
                raise MergeBatchError(f"gh pr create printed no PR url: {first_line(out)}")
            st.update(pr=int(found.group(1)), url=found.group(0))
    previous_head = st.get("head")
    st.update(status="pushed", head=head)
    run_id = args.ci_run or ((st.get("ci") or {}).get("id") if previous_head == head else None)
    st["ci"] = None  # never keep CI evidence across a new pushed head or a failed lookup
    save_state(st)
    if run_id:
        ci = gh_json("run", "view", str(run_id), "--repo", st["repo"], "--json",
                     "databaseId,headSha,status,conclusion")
        if ci.get("headSha") != head or ci.get("databaseId") != run_id:
            raise MergeBatchError(f"CI run {run_id} does not name pushed head {head}")
        st["ci"] = {"id": run_id, "head": head, "state": ci.get("status"), "conclusion": ci.get("conclusion")}
        save_state(st)
        record_part_cost(st, run_id, head)
    print(f"pushed {st['branch']} {head}; {st['url']}; land needs an approval ending in 'HEAD: {head}' and "
          "'VERDICT: PASS'", file=sys.stderr)
    print(st["pr"])
    return 0


def check_approval(path, head):
    """The approval is seat verdict text about this very head, with a fixed ending: free text, then
    ``HEAD: <sha>`` (the batch head in full: all 40 hex digits, any case), then ``VERDICT: PASS`` (or
    ``pass``) as the last non-empty line, with no prefix. A FAIL or CHANGES verdict line anywhere
    refuses it, so a quoted or bulleted ``VERDICT: pass`` cannot flip an earlier ``fix``.
    This parser is deliberately stricter than the keeper's seat tool and does not use it."""
    if not path:
        raise MergeBatchError("land needs --approval PATH (seat verdict text ending in HEAD: <sha> and VERDICT: PASS)")
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MergeBatchError(f"approval file {path} cannot be read: {exc.strerror or exc}") from None
    lines = [line.rstrip() for line in text.split("\n") if line.strip()]  # only \n ends a line (not U+2028 ...)
    for line in lines:
        if BAD_VERDICT.search(line):
            raise MergeBatchError(f"approval file {path} has a FAIL/CHANGES verdict line: {line.strip()[:80]!r}")
    last = lines[-1] if lines else ""
    if last not in PASS_LINES:
        raise MergeBatchError(f"approval file {path}: its last non-empty line must be exactly 'VERDICT: PASS' "
                              f"(no prefix), found {last[:80]!r}")
    found = HEAD_LINE.fullmatch(lines[-2]) if len(lines) > 1 else None
    # the full sha only: a short prefix of the right head could name another commit
    if not found or found.group(1).lower() != head.lower():
        raise MergeBatchError(f"approval file {path}: the line before the verdict must be 'HEAD: <sha>' naming "
                              f"the batch head {head} in full (all 40 hex digits)")


def parse_checks(text):
    """[(name, bucket)] from the piped `gh pr checks` text, one check per line:
    ``name<TAB>bucket<TAB>elapsed<TAB>link<TAB>description`` (gh 2.45 has no `pr checks --json`).

    The columns are read from the right, so a tab inside a check name only lengthens the name and cannot
    move the bucket (a check named ``lint<TAB>pass`` is not a green ``lint``). A line that does not fit, or
    a bucket gh does not use, is an error: unknown is never green."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < 5 or cols[-4] not in BUCKETS:
            raise MergeBatchError(f"unreadable line in the `gh pr checks` output: {line[:80]!r}")
        rows.append(("\t".join(cols[:-4]), cols[-4]))
    return rows


def require_green(repo, number, names):
    """Every required check (or each of --checks) is reported and green on the PR's head.

    The exit code of gh is 1 when a check failed and 8 while one is pending. For the required set it must be
    0 as well as every line green, so a line that fooled the parser still cannot hide a failing check."""
    proc = gh("pr", "checks", str(number), "--repo", repo, *([] if names else ["--required"]), ok=(0, 1, 8))
    rows = parse_checks(proc.stdout)
    if names:
        missing = sorted(set(names) - {name for name, _ in rows})
        if missing:
            raise MergeBatchError("check(s) not reported on the batch head: " + ", ".join(missing))
        rows = [(name, bucket) for name, bucket in rows if name in names]
    if not rows:
        raise MergeBatchError(f"no required check is reported for batch PR #{number} "
                              f"({first_line(proc.stderr)}): wait for CI or name the checks with --checks")
    bad = [f"{name}={bucket}" for name, bucket in rows if bucket not in GREEN]
    if bad:
        pending = [name for name, bucket in rows if bucket == "pending"]
        tail = f" (pending: {', '.join(pending)}; land does not wait or retry, run it again when it ends)" if pending else ""
        raise MergeBatchError("required check(s) not green on the batch head: " + ", ".join(bad) + tail)
    if not names and proc.returncode != 0:
        raise MergeBatchError(f"gh pr checks --required exits {proc.returncode} although every line reads green: "
                              "not trusting the output")


def require_full_run(repo, head, branch):
    """Use Actions evidence at the batch SHA, independent of branch protection/--checks."""
    endpoint = f"repos/{repo}/actions"
    runs = gh_json("api", f"{endpoint}/workflows/tests.yml/runs?head_sha={head}&event=pull_request&per_page=100")["workflow_runs"]
    if not runs:
        raise MergeBatchError("full_ci: no tests run on exact batch head " + head)
    run = max(runs, key=lambda r: r["id"])
    if (run.get("head_sha") != head or run.get("head_branch") != branch
            or run.get("event") != "pull_request" or not branch.startswith("batch/")
            or run.get("status") != "completed" or run.get("conclusion") != "success"):
        raise MergeBatchError(f"full_ci: tests run {run['id']} is not green on exact batch head {head}")
    attempt = run.get("run_attempt", 1)
    def jobs_for(number, run_id=None):
        run_id = run["id"] if run_id is None else run_id
        jobs, page = [], 1
        while True:
            data = gh_json("api", f"{endpoint}/runs/{run_id}/attempts/{number}/jobs?per_page=100&page={page}")
            jobs.extend(data["jobs"])
            if len(jobs) >= data["total_count"]:
                return jobs
            if not data["jobs"]:
                raise MergeBatchError("full_ci: incomplete job pagination")
            page += 1
    jobs = jobs_for(attempt)
    seen = {j["name"] for j in jobs}
    missing = {key for key in JOB_DISPLAY if not job_named(seen, key)}
    missing.update(f"{suite} (shard {i + 1})" for suite in SHARD_DISPLAY for i in range(3)
                   if not shard_named(seen, suite, i))
    bad = [f"{j['name']}={j.get('conclusion') or j.get('status')}" for j in jobs
           if j.get("status") != "completed" or j.get("conclusion") != "success"]
    if missing or bad:
        raise MergeBatchError("full_ci: " + ", ".join([*(f"{n}=missing" for n in sorted(missing)), *bad]))
    reruns = []
    for previous in runs:
        if previous["id"] != run["id"]:
            names = ", ".join(sorted(j["name"] for j in jobs_for(previous.get("run_attempt", 1), previous["id"])))
            reruns.append(f"tests run {run['id']} replaces run {previous['id']} at {head}: {names}")
    for number in range(2, attempt + 1):
        names = ", ".join(sorted(j["name"] for j in jobs_for(number)))
        reruns.append(f"tests run {run['id']} attempt {number} at {head}: {names}")
    return reruns


def require_current_base(repo, head, base="main"):
    """The green run of ``head`` was tested on a tree that contains main's current tip.

    A head green on an older base can still turn main red when other pull requests landed in between
    (two pull requests that grow the same file, each under the limit alone); GitHub's --match-head-commit
    binds the head, not the base. ``behind_by`` of ``base...head`` must be 0."""
    cmp = gh_json("api", f"repos/{repo}/compare/{base}...{head}")
    behind = cmp.get("behind_by")
    if not isinstance(behind, int) or isinstance(behind, bool):
        raise MergeBatchError(f"stale_base: cannot read how far {short(head)} is behind {base}")
    if behind:
        raise MergeBatchError(f"stale_base: green run of {short(head)} was tested on an older {base} "
                              f"(head is {behind} commit(s) behind it); update the head and let CI run again")


def record_reruns(repo, number, reruns):
    if not reruns:
        return
    comments, page = [], 1
    endpoint = f"repos/{repo}/issues/{number}/comments"
    while True:
        batch = gh_json("api", f"{endpoint}?per_page=100&page={page}")
        comments.extend(c.get("body") or "" for c in batch)
        if len(batch) < 100:
            break
        page += 1
    additions = [line for line in reruns if not any(line in body for body in comments)]
    if additions:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "body.md")
            path.write_text("CI reruns (must be explained by a flake fix/quarantine item):\n"
                            + "\n".join("- " + line for line in additions) + "\n", encoding="utf-8")
            # A comment is auditable without triggering pull_request: edited.
            gh("api", "--method", "POST", endpoint, "-F", f"body=@{path}")


def require_main_green(repo, head, go_row=None):
    """Freeze land while main has no completed green full run at its current tip."""
    tip = gh_json("api", f"repos/{repo}/git/ref/heads/main")["object"]["sha"]
    runs = gh_json("api", f"repos/{repo}/actions/workflows/tests.yml/runs?branch=main&per_page=1")["workflow_runs"]
    latest = runs[0] if runs else {}
    if (latest.get("head_sha") == tip and latest.get("head_branch") == "main"
            and latest.get("status") == "completed" and latest.get("conclusion") == "success"):
        return
    if go_row:
        try:
            lines = Path(go_row).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise MergeBatchError(f"main_red: cannot read GO row: {exc}") from None
        for line in lines:
            tokens = line.split()
            if "GO" in tokens and head in tokens and "EXCEPTION:main-red-fix" in tokens:
                print(f"main_red: GO exception for {head} from {go_row}", file=sys.stderr)
                return
    raise MergeBatchError("main_red: latest tests run on main is not completed green at its tip; "
                          "only a GO row for this head with EXCEPTION:main-red-fix permits land")


def settle(repo, recs, wait):
    """{pr: GitHub's state and head} of the included PRs, polled for up to ``wait`` seconds until each is MERGED
    or has a moved head (a PR with a new commit does not merge by itself, so it is not waited for)."""
    deadline = time.monotonic() + wait
    views = {}

    def open_rec(rec):
        view = views.get(rec["pr"])
        return view is None or (view["state"] != "MERGED" and view["headRefOid"] == rec["head"])

    while True:
        for rec in filter(open_rec, recs):
            views[rec["pr"]] = gh_json("pr", "view", str(rec["pr"]), "--repo", repo, "--json", "state,headRefOid")
        if not any(map(open_rec, recs)) or time.monotonic() >= deadline:
            return views
        time.sleep(min(3.0, max(0.0, deadline - time.monotonic())))


def included_pr(repo, number, rec, view, merge):
    """Report one PR of the batch: ``("ok", "")`` after printing its MERGED line, ``("moved", "")`` after its
    NOT-MERGED line, or ``("problem", why)``. A PR GitHub left open is closed only after its head was read once
    more, so a push that came after ``settle`` looked is not closed as merged; a head that moved after the
    build is neither closed nor reported merged, and its author has to update it (needs-update label). Every
    PR is reported MERGED only when its recorded head is an ancestor of the merge commit, whatever GitHub's
    state says."""
    num = rec["pr"]
    if git("merge-base", "--is-ancestor", rec["head"], merge, ok=(0, 1)).returncode != 0:
        return "problem", f"#{num}: head {short(rec['head'])} is not in merge {short(merge)}"  # no MERGED line
    if view["state"] != "MERGED" and view["headRefOid"] == rec["head"]:
        view = gh_json("pr", "view", str(num), "--repo", repo, "--json", "state,headRefOid")
        if view["state"] == "OPEN" and view["headRefOid"] == rec["head"]:
            gh("pr", "close", str(num), "--repo", repo, "--comment", f"merged via batch PR #{number}")
    if view["headRefOid"] != rec["head"]:
        print(f"NOT-MERGED {num} head moved")
        if view["state"] == "OPEN" and not add_label(repo, num, NEEDS_UPDATE):
            print(f"warning: could not add the {NEEDS_UPDATE} label to PR #{num}", file=sys.stderr)
        return "moved", ""
    print(f"MERGED {num} {merge} {','.join(rec['items']) or '-'}")
    return "ok", ""


def cmd_land(args):
    st = load_state(args.name)
    repo, number = st["repo"], args.batch_pr
    if st["status"] != "pushed" or st.get("pr") != number:
        raise MergeBatchError(f"batch {st['name']!r} is {st['status']} with batch PR {st.get('pr')}: "
                              f"push it first and pass its PR number (got #{number})")
    check_approval(args.approval, st["head"])
    require_part_seats(st)
    fields = "state,headRefOid,isDraft,mergeCommit,baseRefName"
    pr = gh_json("pr", "view", str(number), "--repo", repo, "--json", fields)
    if pr["headRefOid"] != st["head"]:
        raise MergeBatchError(f"batch PR #{number} head moved: {short(pr['headRefOid'])} on GitHub, "
                              f"{short(st['head'])} saved at push; review and push again")
    if pr["state"] == "OPEN":
        if pr["isDraft"]:
            raise MergeBatchError(f"batch PR #{number} is a draft")
        if pr.get("baseRefName") != "main":
            raise MergeBatchError("full_ci: batch PR must target main")
        reruns = require_full_run(repo, st["head"], st["branch"])
        require_current_base(repo, st["head"])
        require_green(repo, number, [c for c in (args.checks or "").split(",") if c])
        require_main_green(repo, st["head"], args.go_row)
        record_reruns(repo, number, reruns)
        gh("pr", "merge", str(number), "--repo", repo, "--merge", "--match-head-commit", st["head"],
           "--body", merge_trailers(st))
    elif pr["state"] != "MERGED":
        raise MergeBatchError(f"batch PR #{number} is {pr['state']}")
    for _ in range(10):  # the merge commit can lag the merge by a moment
        pr = gh_json("pr", "view", str(number), "--repo", repo, "--json", fields)
        if pr["state"] == "MERGED" and (pr.get("mergeCommit") or {}).get("oid"):
            break
        time.sleep(min(2.0, args.wait / 5))
    merge = (pr.get("mergeCommit") or {}).get("oid")
    if pr["state"] != "MERGED" or not merge:
        raise MergeBatchError(f"batch PR #{number} is {pr['state']} with no merge commit yet: run land again")
    git("fetch", "--quiet", REMOTE, f"+refs/heads/{st['base']}:refs/remotes/{REMOTE}/{st['base']}", ok=None)
    views = settle(repo, st["merged"], args.wait)
    problems, moved, landed = [], [], []
    for rec in st["merged"]:
        outcome, why = included_pr(repo, number, rec, views[rec["pr"]], merge)
        if outcome == "moved":
            moved.append(rec["pr"])
        elif outcome == "problem":
            problems.append(why)
        else:
            landed.append(rec)
    if problems:
        raise MergeBatchError("batch PR merged, but " + "; ".join(problems) + " (state kept, land again after a fix)")
    if moved:
        print(f"batch PR #{number} merged without " + ", ".join(f"#{n}" for n in moved)
              + f" (head moved after the build; labelled {NEEDS_UPDATE}): they need a new batch", file=sys.stderr)
    for skip in st["skipped"]:
        print(f"not merged: #{skip['pr']} ({skip['reason']})", file=sys.stderr)
    drop_state(st)
    try:
        cleanup_landed(st, landed)
    except Exception as exc:  # Cleanup is strictly best effort after GitHub has landed.
        print(f"cleanup failed: {exc}", file=sys.stderr)
    close_merged_items()
    return 0


def merge_trailers(st):
    """The body of the batch merge commit: one Plan-Item trailer per item of every included part and
    the batch item, then one Review-Run line per keeper-bound part review (key, run id, part head)."""
    keys = {key for rec in st["merged"] for key in part_items(st, rec)}
    batch = st.get("plan")
    if isinstance(batch, str) and re.fullmatch(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+", batch):
        keys.add(batch)
    runs = [f"Review-Run: {key} {run} {rec['head']}" for rec in st["merged"]
            for key, run in sorted((rec.get("review_runs") or {}).items())]
    return "\n".join([f"Plan-Item: {key}" for key in sorted(keys)] + runs)


def close_merged_items():
    """Run the keeper merge-close job once for the landed batch; its report goes to stderr, never fails the land."""
    try:
        proc = run(["plangraph", "merge-close"], ok=None, timeout=330)
    except MergeBatchError as exc:
        print(f"merge-close not run: {exc}", file=sys.stderr)
        return
    sys.stderr.write(proc.stdout + proc.stderr)


def cleanup_landed(st, landed):
    """Best effort cleanup with exact state SHAs; all sweep protections still apply."""
    try:
        repo = git("rev-parse", "--show-toplevel").stdout.strip()
        trees = worktrees()
        targets = [(st["worktree"], st["branch"], st["head"])]
        for rec in landed:
            branch = st["prs"][str(rec["pr"])]["ref"]
            for path, name in trees:
                if name == branch or (not name and git("rev-parse", "HEAD", cwd=path).stdout.strip() == rec["head"]):
                    targets.append((path, name, rec["head"]))
        pins = os.environ.get("PG_PINS", str(Path.home() / ".config/plangraph/pins.json"))
        argv = ["pg-worktree-sweep", "--repo", repo, "--pins", pins, "--apply", "--delete-remote"]
        for path, branch, sha in targets:
            argv.extend(["--root", str(Path(path).parent), "--target", f"{path}:{'refs/heads/' + branch if branch else '-'}:{sha}"])
        for rec in landed:
            branch = st["prs"][str(rec["pr"])]["ref"]
            argv.extend(["--remote-target", f"{branch}:{rec['head']}"])
        result = run(argv)
        print(result.stdout, end="", file=sys.stderr)
    except MergeBatchError as exc:
        print(f"cleanup sweep failed: {exc}", file=sys.stderr)


def cmd_status(args):
    states = all_states()
    where = {branch: path for path, branch in worktrees()}
    orphans = [{"name": name, "status": "orphan", "branch": f"batch/{name}", "worktree": where.get(f"batch/{name}", "-"),
                "merged": [], "skipped": [], "pending": []} for name in orphan_names()]
    if args.json:
        print(json.dumps(states + orphans, indent=2, sort_keys=True))
        return 0
    if not states and not orphans:
        print("no batch in progress")
    for st in states:
        print(f"{st['name']}  {st['status']}  base={st.get('base', '-')}  merged={len(st['merged'])} "
              f"skipped={len(st['skipped'])} pending={len(st['pending'])}  pr={st.get('pr') or '-'}  "
              f"head={short(st.get('head')) or '-'}  {st.get('worktree', '-')}")
        if st.get("current"):
            cur = st["current"]
            print(f"  conflict in PR #{cur['pr']}: {', '.join(cur['files'])}")
        if st["status"] == "creating":
            print(f"  died while its worktree was made: merge_batch.py abort --name {st['name']}, then build again")
        if st.get("merging"):
            print(f"  died while merging PR #{st['merging']['pr']}: merge_batch.py continue --name {st['name']}")
    for orphan in orphans:
        print(f"{orphan['name']}  orphan  no state file, but a build left its PR-head refs ({orphan['branch']}, "
              f"worktree {orphan['worktree']}): merge_batch.py abort --name {orphan['name']}")
    return 0


def part_items(st, rec):
    wt, merge, head = st["worktree"], rec["merge"], rec["head"]
    if git("rev-parse", f"{merge}^2", cwd=wt).stdout.strip() != head:
        raise MergeBatchError(f"part #{rec['pr']} merge does not bind its head")
    from plangraph.keeper.evidence_git import plan_items  # the keeper's reading of a commit's trailers

    messages = git("log", "--format=%B%x1d", f"{merge}^1..{head}", cwd=wt).stdout.split("\x1d")
    keys = {key for message in messages for key in plan_items(message)}
    if not keys or any(not re.fullmatch(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+", key) for key in keys):
        raise MergeBatchError(f"part #{rec['pr']} needs a Plan-Item for its keeper-bound review")
    return keys


def part_seat(run_id, key, head):
    from plangraph.keeper.config import ConfigError, load_config
    from plangraph.keeper.evidence_runs import RunRecordAdapter
    from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter

    if not isinstance(run_id, str) or not run_id.startswith("run-") or not RunRecordAdapter(None).valid_run_id(run_id):
        raise MergeBatchError("a keeper-bound review requires a spawner run id")
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "plangraph"
    try:
        config = load_config(config_dir / "keeper.json")
        adapter = SpawnerRecordAdapter(config.spawner_records_dir)
        seats = adapter.review_seats({"key": key, "run_ids": json.dumps([run_id])}, head, "",
                                     allow_cli_reported=True, require_diff_head=True,
                                     require_explicit_accept=True)
        if len(seats) != 1 or seats[0]["verdict"] != "pass":
            raise ValueError(f"{run_id}: expected one passing review seat")
    except (ConfigError, OSError, ValueError) as exc:
        raise MergeBatchError(f"keeper-bound review for {key}: {exc}") from None


def require_part_seats(st):
    if not st["merged"]:
        raise MergeBatchError("batch has no included parts")
    seats = {}
    for rec in st["merged"]:
        if git("merge-base", "--is-ancestor", rec["head"], st["head"],
               cwd=st["worktree"], ok=(0, 1)).returncode:
            raise MergeBatchError(f"part #{rec['pr']} is not in batch head")
        keys = part_items(st, rec)
        recorded = rec.get("review_runs") or {}
        if not isinstance(recorded, dict) or set(recorded) != keys:
            raise MergeBatchError(f"part #{rec['pr']} lacks a keeper-bound review on {rec['head']}")
        for key in sorted(keys):
            part_seat(recorded[key], key, rec["head"])
        review = rec.get("review")
        if review and (review.get("verdict") != "ACCEPT" or review.get("head") != rec["head"]
                       or part_review(review.get("file", ""), rec["head"])["verdict"] != "ACCEPT"):
            raise MergeBatchError(f"part #{rec['pr']} supplementary review does not accept its head")
        seats[rec["pr"]] = [recorded[key] for key in sorted(keys)]
    return seats


def cmd_review(args):
    st = load_state(args.name)
    rec = next((r for r in st["merged"] if r["pr"] == args.pr), None)
    if rec is None:
        raise MergeBatchError(f"PR #{args.pr} is not an included part")
    if args.file:
        rec["review"] = part_review(args.file, rec["head"])
    else:
        reason = None
        for key in sorted(part_items(st, rec)):
            try:
                part_seat(args.run_id, key, rec["head"])
            except MergeBatchError as exc:
                reason = exc
                continue
            rec.setdefault("review_runs", {})[key] = args.run_id
            break
        else:
            raise reason
    save_state(st)
    return 0


def cmd_go_req(args):
    st = load_state(args.name)
    if st["status"] != "pushed" or not st.get("pr") or not st.get("base_head"):
        raise MergeBatchError("go-req needs a pushed batch with a recorded base head")
    wt, head = st["worktree"], st["head"]
    if git("rev-parse", "HEAD", cwd=wt).stdout.strip() != head:
        raise MergeBatchError("batch head moved since push; push again and refresh CI")
    live = gh_json("pr", "view", str(st["pr"]), "--repo", st["repo"], "--json", "state,headRefOid")
    if live.get("state") != "OPEN" or live.get("headRefOid") != head:
        raise MergeBatchError("batch PR is not open on the saved pushed head; push again")
    plan = st.get("plan") or ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+", plan):
        raise MergeBatchError("build needs --plan <keeper key> for the batch PLAN block")
    parts = []
    seats = require_part_seats(st)
    for rec in st["merged"]:
        runs = ",".join(seats[rec["pr"]])
        parts.append(f"#{rec['pr']} sha={rec['head']} review={runs} verdict=ACCEPT")
    ci = st.get("ci") or {}
    if (ci.get("head") != head or not ci.get("id") or ci.get("state") != "completed" or
            ci.get("conclusion") != "success"):
        raise MergeBatchError("batch CI is not green on pushed head; push --ci-run ID after full CI completes")
    batch_workflow_gate(st)
    stat = git("diff", "--shortstat", st["base_head"], head, cwd=wt).stdout.strip()
    declared = st.get("deviations", "none").strip() or "none"
    deviations = [declared] if declared != "none" else []
    deviations += st.get("forced", [])
    deviation = "; ".join(deviations) or "none"
    line = (f"GO-REQ #{st['pr']} {st['repo']} head {head}; diff stat: {stat}; parts: " + "; ".join(parts) +
            f"; CI run {ci['id']} state={ci['state']} conclusion={ci['conclusion']}; "
            f"PLAN {plan} conforms={'no' if deviations else 'yes'} deviations={deviation}"
            + (f"; {windows_report(st, head)}" if windows_report(st, head) else ""))
    print(" ".join(line.split()))
    return 0
