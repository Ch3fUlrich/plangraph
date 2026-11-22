#!/usr/bin/env python3
"""Canned `gh` for tests/test_merge_batch.py (D-852): never talks to a network.

Serves JSON from $GH_STUB_DIR/state.json, appends every call to calls.jsonl, and merges batch
PRs inside the local bare repo named there (git plumbing). A call it does not know exits 97
and leaves UNEXPECTED, which the test fixture turns into a failure.
"""
import json
import os
import re
import signal
import subprocess
import sys

DIR = os.environ["GH_STUB_DIR"]
ARGV = sys.argv[1:]
#: the real `gh pr ... --json` names (2.45): a misspelt field must fail here, not on a laptop.
PR_FIELDS = set("""additions assignees author autoMergeRequest baseRefName body changedFiles closed closedAt
comments commits createdAt deletions files headRefName headRefOid headRepository headRepositoryOwner id
isCrossRepository isDraft labels latestReviews maintainerCanModify mergeCommit mergeStateStatus mergeable
mergedAt mergedBy milestone number potentialMergeCommit reviewDecision reviews state statusCheckRollup
title updatedAt url""".split())
S = json.load(open(os.path.join(DIR, "state.json")))
with open(os.path.join(DIR, "calls.jsonl"), "a") as log:
    log.write(json.dumps(ARGV) + "\n")


def opt(flag):
    return ARGV[ARGV.index(flag) + 1] if flag in ARGV else None


def die(message, rc=1):
    print(message, file=sys.stderr)
    sys.exit(rc)


def fields(allowed):
    names = (opt("--json") or "").split(",")
    if not names[0] or set(names) - allowed:
        die(f"unknown json field in {names}", 1)
    return names


def git(*args, ok=(0,)):
    proc = subprocess.run(["git", "--git-dir", S["origin"], *args], capture_output=True, text=True)
    if proc.returncode not in ok:
        die(f"git {args[0]}: {proc.stderr.strip()}")
    return proc


def save():
    json.dump(S, open(os.path.join(DIR, "state.json"), "w"))


def follow_branch(pr):
    """GitHub moves a PR's head with its branch: PRs created through this stub follow the origin."""
    if pr.get("live"):
        pr["headRefOid"] = git("rev-parse", f"refs/heads/{pr['headRefName']}").stdout.strip()
    return pr


def show(pr):
    follow_branch(pr)
    return {name: pr.get(name) for name in fields(PR_FIELDS)}


def crash_after(call):
    """Tests: kill the tool (the parent process) right after ``call`` did its work, like an OOM kill."""
    if S.get("die_after") == call:
        os.kill(os.getppid(), signal.SIGKILL)


def pr_arg():
    return follow_branch(S["prs"].get(ARGV[2]) or die(f"no pull request {ARGV[2]}"))


cmd = ARGV[:2]
if cmd == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": S["repo"], "defaultBranchRef": {"name": S["default_branch"]}}))
elif cmd == ["run", "view"]:
    run = S.get("runs", {}).get(ARGV[2]) or die("no CI run")
    print(json.dumps({name: run.get(name) for name in fields(set("databaseId headSha status conclusion".split()))}))
elif cmd == ["pr", "list"]:
    label, head = opt("--label"), opt("--head")
    print(json.dumps([show(p) for p in S["prs"].values() if p["state"] == "OPEN"
                      and (label is None or label in [x["name"] for x in p["labels"]])
                      and (head is None or head == p["headRefName"])]))
elif cmd == ["pr", "view"]:
    pr = pr_arg()
    out = show(pr)
    move = S.get("move_after_view", {}).get(ARGV[2])  # tests: the PR gets a new commit right after this view
    if move:
        move["seen"] = move.get("seen", 0) + 1
        if move["seen"] == move["after"]:
            pr["headRefOid"] = move["to"]
        save()
    print(json.dumps(out))
elif cmd == ["pr", "edit"]:  # gh 2.45 fails here for every repo: the tool must use the REST API
    die("GraphQL: Projects (classic) is being deprecated in favor of the new Projects experience "
        "(repository.pullRequest.projectCards)")
elif ARGV[0] == "api":  # the REST calls the tool makes: add labels, update title and body
    endpoint_get = ARGV[1]
    if "/git/ref/heads/main" in endpoint_get:
        print(json.dumps({"object": {"sha": git("rev-parse", "refs/heads/main").stdout.strip()}}))
        sys.exit(0)
    if "/compare/" in endpoint_get:  # compare/main...HEAD: how far the head is behind main
        head_sha = endpoint_get.split("...", 1)[1]
        behind = S.get("behind_by")
        if behind is None:
            behind = int(git("rev-list", "--count", f"{head_sha}..refs/heads/main").stdout.strip() or 0)
        print(json.dumps({"behind_by": behind, "ahead_by": 1}))
        sys.exit(0)
    if re.search(r"/issues/\d+/events", endpoint_get):  # label events of a PR (the READY time of a part)
        print(json.dumps(S.get("events", {}).get(re.search(r"/issues/(\d+)/", endpoint_get).group(1), [])))
        sys.exit(0)
    if "/actions/" in endpoint_get:
        batch = next((follow_branch(p) for p in S["prs"].values() if p["headRefName"].startswith("batch/")), None)
        run = {"id": 7, "head_sha": batch["headRefOid"] if batch else "0" * 40,
               "head_branch": batch["headRefName"] if batch else "main", "event": "pull_request",
               "status": "completed", "conclusion": "success", "run_attempt": 1,
               "run_started_at": "2026-11-21T09:50:00Z", "updated_at": "2026-11-21T10:01:00Z"}
        if "head_sha=" in endpoint_get:
            sha = endpoint_get.split("head_sha=", 1)[1].split("&", 1)[0]
            matches = [pr for pr in map(follow_branch, S["prs"].values()) if pr["headRefOid"] == sha]
            runs = [
                dict(run, id=pr["number"], head_sha=sha, head_branch=pr["headRefName"],
                     pull_requests=[{"number": pr["number"]}],
                     conclusion=pr["statusCheckRollup"][0]["conclusion"].lower())
                for pr in matches if pr["statusCheckRollup"] and pr["statusCheckRollup"][0]["conclusion"]
            ]
            if batch and batch in matches and not batch["statusCheckRollup"]:
                runs.append(dict(run, pull_requests=[{"number": batch["number"]}]))
            print(json.dumps({"workflow_runs": runs}))
            sys.exit(0)
        if "branch=main" in endpoint_get:
            run.update(head_sha=git("rev-parse", "refs/heads/main").stdout.strip(), head_branch="main", event="push")
        if "/jobs?" in endpoint_get:
            names = ["Select suites for this change", "Public scrub: no private names or secrets",
                     "File size limit (400 lines)", "New code is Rust", "Changelog fragment and docs",
                     "UI tests in headless Chromium", "Rust workspace on Linux: fmt, clippy, tests",
                     "Rust spawner and worker core tests on Windows", "Rust worker crate: fmt, clippy, tests",
                     "Rust replay spike tests", "ci-gate"]
            names += [f"Python tests, SQLite keeper (shard {i} of 3)" for i in (1, 2, 3)]
            names += [f"Python tests, Postgres keeper: contract and storage (shard {i} of 3)" for i in (1, 2, 3)]
            jobs = [{"name": n, "status": "completed", "conclusion": "success"} for n in names]
            print(json.dumps({"jobs": jobs, "total_count": len(jobs)}))
        else:
            print(json.dumps({"workflow_runs": [run], "total_count": 1}))
        sys.exit(0)
    method = opt("--method") or "GET"
    endpoint = ARGV[ARGV.index("--method") + 2] if "--method" in ARGV else ""
    fields = [ARGV[i + 1] for i, arg in enumerate(ARGV) if arg in ("-f", "-F")]
    target = re.fullmatch(r"repos/[^/]+/[^/]+/(issues|pulls)/(\d+)(/labels)?", endpoint)
    pr = S["prs"].get(target.group(2)) if target else None
    if pr is None or not ((method, bool(target.group(3))) in (("POST", True), ("PATCH", False))):
        die(f"gh stub: api call not served: {ARGV}", 97)
    if method == "POST":
        label = fields[0].split("=", 1)[1]
        if S.get("label_fail") or label not in S["labels"]:
            die("HTTP 422: Validation Failed (label does not exist)")
        pr["labels"].append({"name": label})
    else:
        for field in fields:
            key, value = field.split("=", 1)
            pr[key] = open(value[1:]).read() if value.startswith("@") else value
    save()
    print(S.get("label_response", json.dumps(pr["labels"])) if method == "POST" else "{}")
elif cmd == ["label", "create"]:
    if S.get("label_fail"):
        die("HTTP 403: Resource not accessible")
    S["labels"].append(ARGV[2])
    save()
elif cmd == ["pr", "create"]:
    number, S["next_pr"] = S["next_pr"], S["next_pr"] + 1
    head = git("rev-parse", f"refs/heads/{opt('--head')}").stdout.strip()
    S["prs"][str(number)] = {
        "number": number, "title": opt("--title"), "body": open(opt("--body-file")).read(),
        "headRefName": opt("--head"), "headRefOid": head, "baseRefName": opt("--base"), "state": "OPEN",
        "isDraft": False, "isCrossRepository": False, "labels": [], "files": [], "additions": 0,
        "deletions": 0, "statusCheckRollup": [], "mergeCommit": None, "live": True,
        "url": f"https://example.invalid/{S['repo']}/pull/{number}"}
    save()
    crash_after("pr create")
    if S.get("create_noise"):  # real gh warns about uncommitted changes around the url
        print("Warning: 1 uncommitted change")
    print(f"https://example.invalid/{S['repo']}/pull/{number}")
elif cmd == ["pr", "checks"]:
    if "--json" in ARGV:  # gh 2.45 has no JSON output for pr checks
        die("unknown flag: --json")
    rows = S["checks"].get(ARGV[2], [])
    if not rows:
        die("no required checks reported on the 'batch' branch")
    for row in rows:  # the piped format: name, bucket, elapsed, link, description
        print(f"{row['name']}\t{row['bucket']}\t0\thttps://example.invalid/run\t{row.get('description', '')}")
    buckets = [row["bucket"] for row in rows]
    sys.exit(0 if all(b in ("pass", "skipping") for b in buckets) else 8 if "pending" in buckets else 1)
elif cmd == ["pr", "merge"]:
    pr = pr_arg()
    if pr["state"] != "OPEN" or "--merge" not in ARGV:
        die("pull request is not open or no --merge given")
    if opt("--match-head-commit") != pr["headRefOid"]:
        die("head commit does not match --match-head-commit")
    head, base = pr["headRefOid"], f"refs/heads/{pr['baseRefName']}"
    tree = git("rev-parse", f"{head}^{{tree}}").stdout.strip()  # base has not moved in these tests
    subject = opt("--subject") or f"Merge pull request #{pr['number']}"
    merge = git("commit-tree", tree, "-p", git("rev-parse", base).stdout.strip(), "-p", head,
                "-m", subject,
                *(["-m", opt("--body")] if opt("--body") else [])).stdout.strip()
    git("update-ref", base, merge)
    for other in S["prs"].values():  # GitHub marks a PR merged once its head is on the base
        if other["state"] == "OPEN" and other["number"] not in S["no_auto_merge"] \
                and git("merge-base", "--is-ancestor", other["headRefOid"], merge, ok=(0, 1)).returncode == 0:
            other.update(state="MERGED", mergeCommit={"oid": merge})
    pr.update(state="MERGED", mergeCommit={"oid": merge})
    save()
elif cmd == ["pr", "close"]:
    pr = pr_arg()
    pr.update(state="CLOSED", closed_with=opt("--comment"))
    save()
else:
    open(os.path.join(DIR, "UNEXPECTED"), "a").write(" ".join(ARGV) + "\n")
    die(f"gh stub: unexpected call {ARGV}", 97)
