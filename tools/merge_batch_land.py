"""push / land / status for tools/merge_batch.py: the batch PR, its gate checks, the MERGED lines."""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path

from merge_batch_util import (REMOTE, MergeBatchError, all_states, drop_state, first_line, gh, gh_json, git,
                              load_state, save_state, short)

#: gh pr checks buckets that satisfy a required check (a skipped required check passes on GitHub too).
GREEN = ("pass", "skipping")


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
    resolved = [rec for rec in st["merged"] if rec["resolved"]]
    if resolved:
        body += ["", "Resolution commits (conflicts resolved by hand, review these hunks):"]
        body += [f"- `{short(rec['merge'])}` PR #{rec['pr']}: {', '.join(f'`{f}`' for f in rec['files'])}"
                 for rec in resolved]
    return "\n".join(body) + "\n"


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
    git("push", REMOTE, f"refs/heads/{st['branch']}:refs/heads/{st['branch']}", cwd=wt)
    title = f"batch {st['name']}: " + " ".join(f"#{rec['pr']}" for rec in st["merged"])
    with tempfile.TemporaryDirectory() as tmp:
        body = Path(tmp, "body.md")
        body.write_text(render_body(st), encoding="utf-8")
        if st.get("pr"):
            if gh_json("pr", "view", str(st["pr"]), "--repo", st["repo"], "--json", "state")["state"] != "OPEN":
                raise MergeBatchError(f"batch PR #{st['pr']} is not open any more: abort and build again")
            gh("pr", "edit", str(st["pr"]), "--repo", st["repo"], "--title", title, "--body-file", str(body))
        else:
            url = first_line(gh("pr", "create", "--repo", st["repo"], "--base", st["base"], "--head", st["branch"],
                                "--title", title, "--body-file", str(body)).stdout)
            found = re.search(r"/pull/(\d+)", url)
            if not found:
                raise MergeBatchError(f"gh pr create printed no PR url: {url}")
            st.update(pr=int(found.group(1)), url=url)
    st.update(status="pushed", head=head)
    save_state(st)
    print(f"pushed {st['branch']} {short(head)}; {st['url']}", file=sys.stderr)
    print(st["pr"])
    return 0


def check_approval(path):
    """The approval file is seat verdict text: the last VERDICT line must be a pass."""
    if not path:
        raise MergeBatchError("land needs --approval PATH (seat verdict text)")
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MergeBatchError(f"approval file {path} cannot be read: {exc.strerror or exc}") from None
    try:
        from seat_verdict import parse_verdict  # the parser the keeper's seat tool uses
    except ImportError:
        raise MergeBatchError("tools/seat_verdict.py is not importable: cannot read the approval") from None
    if parse_verdict(text)[0] is not True:
        raise MergeBatchError(f"approval file {path} has no passing VERDICT line (the last VERDICT line decides)")


def require_green(repo, number, names):
    """Every required check (or each of --checks) is reported and green on the PR's head."""
    cmd = ["pr", "checks", str(number), "--repo", repo, "--json", "name,bucket,state"]
    proc = gh(*cmd, *([] if names else ["--required"]), ok=(0, 1, 8))  # 1 failing, 8 pending
    try:
        rows = json.loads(proc.stdout)
    except ValueError:
        raise MergeBatchError(f"cannot read the checks of PR #{number}: "
                              f"{first_line(proc.stderr or proc.stdout)}") from None
    if names:
        seen = {row["name"] for row in rows}
        if set(names) - seen:
            raise MergeBatchError("check(s) not reported on the batch head: " + ", ".join(sorted(set(names) - seen)))
        rows = [row for row in rows if row["name"] in names]
    if not rows:
        raise MergeBatchError(f"no required check is reported for batch PR #{number}: wait for CI "
                              "or name the checks with --checks")
    bad = [f"{row['name']}={row['bucket']}" for row in rows if row["bucket"] not in GREEN]
    if bad:
        raise MergeBatchError("required check(s) not green on the batch head: " + ", ".join(bad))


def settle(repo, numbers, wait):
    """PR numbers still not MERGED after polling for ``wait`` seconds."""
    deadline = time.monotonic() + wait
    todo = list(numbers)
    while True:
        todo = [n for n in todo if gh_json("pr", "view", str(n), "--repo", repo, "--json", "state")["state"] != "MERGED"]
        if not todo or time.monotonic() >= deadline:
            return todo
        time.sleep(min(3.0, max(0.0, deadline - time.monotonic())))


def cmd_land(args):
    st = load_state(args.name)
    repo, number = st["repo"], args.batch_pr
    if st["status"] != "pushed" or st.get("pr") != number:
        raise MergeBatchError(f"batch {st['name']!r} is {st['status']} with batch PR {st.get('pr')}: "
                              f"push it first and pass its PR number (got #{number})")
    check_approval(args.approval)
    fields = "state,headRefOid,isDraft,mergeCommit"
    pr = gh_json("pr", "view", str(number), "--repo", repo, "--json", fields)
    if pr["headRefOid"] != st["head"]:
        raise MergeBatchError(f"batch PR #{number} head moved: {short(pr['headRefOid'])} on GitHub, "
                              f"{short(st['head'])} saved at push; review and push again")
    if pr["state"] == "OPEN":
        if pr["isDraft"]:
            raise MergeBatchError(f"batch PR #{number} is a draft")
        require_green(repo, number, [c for c in (args.checks or "").split(",") if c])
        gh("pr", "merge", str(number), "--repo", repo, "--merge", "--match-head-commit", st["head"])
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
    left = settle(repo, [rec["pr"] for rec in st["merged"]], args.wait)
    problems = []
    for rec in st["merged"]:
        if rec["pr"] in left:
            if git("merge-base", "--is-ancestor", rec["head"], merge, ok=(0, 1)).returncode != 0:
                problems.append(f"#{rec['pr']}: head {short(rec['head'])} is not in merge {short(merge)}")
                continue
            gh("pr", "close", str(rec["pr"]), "--repo", repo, "--comment", f"merged via batch PR #{number}")
        print(f"MERGED {rec['pr']} {merge} {','.join(rec['items']) or '-'}")
    if problems:
        raise MergeBatchError("batch PR merged, but " + "; ".join(problems) + " (state kept, land again after a fix)")
    for skip in st["skipped"]:
        print(f"not merged: #{skip['pr']} ({skip['reason']})", file=sys.stderr)
    drop_state(st)
    print(f"cleanup when done: git worktree remove {st['worktree']} && git branch -D {st['branch']}", file=sys.stderr)
    return 0


def cmd_status(args):
    states = all_states()
    if args.json:
        print(json.dumps(states, indent=2, sort_keys=True))
        return 0
    if not states:
        print("no batch in progress")
    for st in states:
        print(f"{st['name']}  {st['status']}  base={st.get('base', '-')}  merged={len(st['merged'])} "
              f"skipped={len(st['skipped'])} pending={len(st['pending'])}  pr={st.get('pr') or '-'}  "
              f"head={short(st.get('head')) or '-'}  {st.get('worktree', '-')}")
        if st.get("current"):
            cur = st["current"]
            print(f"  conflict in PR #{cur['pr']}: {', '.join(cur['files'])}")
    return 0
