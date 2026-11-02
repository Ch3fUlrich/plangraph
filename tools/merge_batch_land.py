"""push / land / status for tools/merge_batch.py: the batch PR, its gate checks, the MERGED lines."""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path

from merge_batch_util import (NEEDS_UPDATE, REMOTE, MergeBatchError, add_label, all_states, drop_state, first_line, gh,
                              gh_json, git, load_state, orphan_names, save_state, short, update_pr, worktrees)

#: gh pr checks buckets that satisfy a required check (a skipped required check passes on GitHub too).
GREEN = ("pass", "skipping")
#: Every bucket gh prints; any other word is unknown, and unknown is never green.
BUCKETS = ("pass", "fail", "pending", "skipping", "cancel")
#: A verdict line that says fail, whatever precedes it (quote mark, bullet, bold, underscores): it refuses the approval.
BAD_VERDICT = re.compile(r"(?<![A-Za-z0-9])verdict[\W_]*:[\W_]*(?:fail|fix|halt|reject|change|request[\W_]*change)",
                         re.IGNORECASE)
HEAD_LINE = re.compile(r"HEAD: ([0-9a-fA-F]{12,40})")
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
        if not st.get("pr"):
            adopt_open_pr(st)
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
    st.update(status="pushed", head=head)
    save_state(st)
    print(f"pushed {st['branch']} {head}; {st['url']}; land needs an approval ending in 'HEAD: {head}' and "
          "'VERDICT: PASS'", file=sys.stderr)
    print(st["pr"])
    return 0


def check_approval(path, head):
    """The approval is seat verdict text about this very head, with a fixed ending: free text, then
    ``HEAD: <sha>`` (the batch head, in full or 12+ hex digits), then ``VERDICT: PASS`` (or
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
    if not found or not head.startswith(found.group(1).lower()):
        raise MergeBatchError(f"approval file {path}: the line before the verdict must be 'HEAD: <sha>' naming "
                              f"the batch head {head} (in full, or at least 12 hex digits)")


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
        raise MergeBatchError("required check(s) not green on the batch head: " + ", ".join(bad))
    if not names and proc.returncode != 0:
        raise MergeBatchError(f"gh pr checks --required exits {proc.returncode} although every line reads green: "
                              "not trusting the output")


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
    build is neither closed nor reported merged, and its author has to update it (needs-update label)."""
    num = rec["pr"]
    if view["state"] != "MERGED" and view["headRefOid"] == rec["head"]:
        if git("merge-base", "--is-ancestor", rec["head"], merge, ok=(0, 1)).returncode != 0:
            return "problem", f"#{num}: head {short(rec['head'])} is not in merge {short(merge)}"
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
    views = settle(repo, st["merged"], args.wait)
    problems, moved = [], []
    for rec in st["merged"]:
        outcome, why = included_pr(repo, number, rec, views[rec["pr"]], merge)
        if outcome == "moved":
            moved.append(rec["pr"])
        elif outcome == "problem":
            problems.append(why)
    if problems:
        raise MergeBatchError("batch PR merged, but " + "; ".join(problems) + " (state kept, land again after a fix)")
    if moved:
        print(f"batch PR #{number} merged without " + ", ".join(f"#{n}" for n in moved)
              + f" (head moved after the build; labelled {NEEDS_UPDATE}): they need a new batch", file=sys.stderr)
    for skip in st["skipped"]:
        print(f"not merged: #{skip['pr']} ({skip['reason']})", file=sys.stderr)
    drop_state(st)
    print(f"cleanup when done: git worktree remove {st['worktree']} && git branch -D {st['branch']}", file=sys.stderr)
    return 0


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
