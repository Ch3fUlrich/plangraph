"""push / land / status for tools/merge_batch.py: the batch PR, its gate checks, the MERGED lines."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from merge_batch_build import (
    check_landed_parts, cmd_consumed, exclude_consumed, frozen_landing, in_history, landing_journal, landing_lease, landing_mode_of,
    landing_report, main_tip, prepare_train, read_verified_merge, record_part_cost, record_stage,
    require_tested_ff_evidence, run_timing_import, seats_acknowledged, wait_marked_merged,
)
from merge_batch_util import (landing_now, landing_batch_key, keeper_landing_configured,
                              TESTED_FF, REMOTE, MergeBatchError, all_states, drop_state, first_line, gh,
                              gh_json, git, load_state, orphan_names, save_state, short, update_pr, worktrees,
                              batch_workflow_gate, keeper_migration, part_review, run, write_landing, write_landing_records,
                              close_merged_items, active_train, go_key_block, part_items, train_keys)
from merge_batch_plan import cleanup_landed, hold_lane_refs, writers_stopped, windows_commands, windows_gate, windows_needed, windows_report, windows_script, workflow_attempt_jobs

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
        pr = st["prs"][str(rec["pr"])]
        title = cell(pr["title"])
        if run_id := pr.get("full_ci_run"):
            title += f" (Full CI run {run_id})"
        rows.append(f"| #{rec['pr']} | {cell(', '.join(rec['items']) or '-')} | `{short(rec['head'])}` | "
                    f"`{short(rec['merge'])}` | {title} |")
    body = [f"Merge batch `{st['name']}` into `{st['base']}`: {len(st['merged'])} PRs, merged with `--no-ff` "
            "in this order.", "", *rows, "", go_key_block(st)]
    if st.get("dropped"):
        body += ["", "Dropped after merge: " + ", ".join(f"#{p['pr']}" for p in st["dropped"])]
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
    active = active_train(st, head)
    if not active["merged"]:
        raise MergeBatchError("no parts remain in the train")
    started = landing_now()
    if keeper_landing_configured():
        os.environ.update(PG_LANDING_REPO=st["repo"], PG_LANDING_BATCH=landing_batch_key(st),
                          PG_LANDING_STAGE_HEAD=head)
    if not st.get("pr"):
        adopt_open_pr(st)
    workflow = Path(wt, ".github/workflows/tests.yml")
    if workflow.is_file():   # a red Windows run refuses the push before anything leaves the machine
        windows_gate(st, workflow.read_text(encoding="utf-8"))
    body_text = render_body(active)
    git("push", REMOTE, f"refs/heads/{st['branch']}:refs/heads/{st['branch']}", cwd=wt)
    if keeper_landing_configured():
        record_stage(st["repo"], landing_batch_key(st), "push", head, started)
    title = f"batch {st['name']}: " + " ".join(f"#{rec['pr']}" for rec in active["merged"])
    with tempfile.TemporaryDirectory() as tmp:
        body = Path(tmp, "body.md")
        body.write_text(body_text, encoding="utf-8")
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
    if keeper_landing_configured():
        with landing_lease(st["repo"]) as lease:
            prepare_train(dict(st, merged=active["merged"]), lease)
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


def full_job_problems(jobs):
    """Require every full-workflow job and shard to finish successfully."""
    seen = {j["name"] for j in jobs}
    missing = {key for key in JOB_DISPLAY if not job_named(seen, key)}
    missing.update(f"{suite} (shard {i + 1})" for suite in SHARD_DISPLAY for i in range(3)
                   if not shard_named(seen, suite, i))
    bad = [f"{j['name']}={j.get('conclusion') or j.get('status')}" for j in jobs
           if j.get("status") != "completed" or j.get("conclusion") != "success"]
    return [*(f"{n}=missing" for n in sorted(missing)), *bad]


def require_full_run(repo, head, branch, batch_key=None):
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
        return workflow_attempt_jobs(endpoint, run_id or run["id"], number, gh_json)
    if problems := full_job_problems(jobs_for(attempt)):
        raise MergeBatchError("full_ci: " + ", ".join(problems))
    reruns = []
    for previous in runs:
        if previous["id"] != run["id"]:
            names = ", ".join(sorted(j["name"] for j in jobs_for(previous.get("run_attempt", 1), previous["id"])))
            reruns.append(f"tests run {run['id']} replaces run {previous['id']} at {head}: {names}")
    for number in range(2, attempt + 1):
        names = ", ".join(sorted(j["name"] for j in jobs_for(number)))
        reruns.append(f"tests run {run['id']} attempt {number} at {head}: {names}")
    if batch_key:
        run_timing_import(repo, batch_key, head, run, attempt)
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


def main_admission(repo, exception=None, binary=None):
    """Ask ``plangraph main-state --admit`` (the one state function of main) and raise its refusal word.

    ``exception`` is the GO row's ``EXCEPTION:main-red-fix`` token; the function honours it only while main is
    red at this read. A missing binary or a failed read refuses as ``state_unknown``."""
    binary = binary or os.environ.get("PLANGRAPH_BIN") or shutil.which("plangraph")
    if not binary:
        raise MergeBatchError("state_unknown: the plangraph binary is not on PATH (set PLANGRAPH_BIN)")
    argv = [binary, "main-state", "--repo", repo, "--admit"] + (["--exception", exception] if exception else [])
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MergeBatchError(f"state_unknown: main-state did not run: {exc}") from None
    if proc.returncode == 0:
        return
    word = (proc.stderr.strip().splitlines() or ["state_unknown"])[-1]
    if proc.returncode != 3 or word not in ("main_red", "main_pending", "state_unknown", "ref_not_forward_only"):
        word = "state_unknown"
    raise MergeBatchError(f"{word}: main's state forbids landing (plangraph main-state --admit); "
                          "only a GO row for this head with EXCEPTION:main-red-fix passes, and only while main is red")


def require_main_green(repo, head, go_row=None):
    """Freeze land while main is not green; the GO row exception is passed to the state function."""
    exception = None
    if go_row:
        try:
            lines = Path(go_row).read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            tokens = line.split()
            if "GO" in tokens and head in tokens and "EXCEPTION:main-red-fix" in tokens:
                exception = "EXCEPTION:main-red-fix"
    main_admission(repo, exception)


#: git's own words for a server that answered no (a rejected ref, a stale lease, a protection or hook refusal):
#: anything else that fails a push is an unknown delivery, not a refusal.
PUSH_REFUSED = re.compile(r"\[(?:remote )?rejected\]|remote rejected|stale info|protected branch|GH\d{3}|"
                          r"declined|denied", re.IGNORECASE)
PUSH_TRIES = 3


def push_tested(st, go_row=None):
    """Land mode ``tested-ff``: the base branch moves to the batch head, the commit that passed full CI, by one
    ``git push --force-with-lease=refs/heads/<base>:<B> origin <head>:refs/heads/<base>``, B the tested base the
    batch was built on. The server applies it only while the branch is exactly B; the lease is only the
    compare-and-set of a fast-forward (the head descends from B, nothing is rewritten) and there is no second
    merge commit. The outcome is read from the branch, never from the push alone:

    * the head is in the branch's history (after an answer, a lost answer or an earlier land): landed, no push;
    * the branch is at another commit than B, head not in it: ``base_moved`` (final, also when it moved into a
      commit the batch contains: the lease names B);
    * the server refused while the branch is B (git says rejected/protected/declined): final, not retried;
    * no answer (anything else that fails): the same push again while the branch is B, at most PUSH_TRIES."""
    head, base, wt = st["head"], st["base"], st["worktree"]
    for attempt in range(1, PUSH_TRIES + 1):
        tip = main_tip(st)
        if in_history(st, head, tip):
            return
        if tip != st["base_head"]:
            raise MergeBatchError(f"base_moved: {base} is at {short(tip)}, the tested base was {short(st['base_head'])}: "
                                  f"the batch head cannot be pushed as a fast-forward; build the batch again")
        require_main_green(st["repo"], head, go_row)
        proc = run(["git", "push", REMOTE, f"--force-with-lease=refs/heads/{base}:{st['base_head']}",
                    f"{head}:refs/heads/{base}"], cwd=wt, ok=None, timeout=None)
        sys.stderr.write(proc.stderr or "")
        if proc.returncode and PUSH_REFUSED.search((proc.stderr or "") + (proc.stdout or "")):
            tip = main_tip(st)  # a late landing of this very head counts
            if in_history(st, head, tip):
                return
            if tip != st["base_head"]:  # the lease did its work: main moved after the read
                raise MergeBatchError(f"base_moved: {base} moved to {short(tip)} while the push was sent, the tested "
                                      f"base was {short(st['base_head'])}; build the batch again")
            raise MergeBatchError(f"refused: origin rejected the push of {short(head)} to {base} while it was "
                                  f"at {short(st['base_head'])}: {first_line((proc.stderr or '') + (proc.stdout or ''))}"
                                  " (the batch stays, land again after the cause is fixed)")
    tip = main_tip(st)
    if not in_history(st, head, tip):
        raise MergeBatchError(f"no answer from origin after {PUSH_TRIES} pushes of {short(head)}: delivery unknown, "
                              f"{base} is at {short(tip)}; land again")


def keeper_config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "plangraph" / "keeper.json"


def require_migration_go(st, go_row):
    version = keeper_migration(st)
    if version is None:
        return
    from plangraph.keeper.store_waiver import _go_binding, _go_statement

    marker = f"MIGRATION {version}"
    refusal = f"GO row for PR #{st['pr']} at {st['head']} must include {marker}"
    if not go_row:
        raise MergeBatchError(refusal)
    try:
        lines = Path(go_row).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MergeBatchError(f"{refusal}: {exc}") from None
    expected = st["pr"], st["head"]
    repo = st["repo"]
    project = repo.rsplit("/", 1)[-1]
    for line in lines:
        cells = line.strip().split("|")
        decision = cells[3].strip() if len(cells) > 4 and not cells[0] else line.strip()
        statement = _go_statement(decision)
        if statement and any(_go_binding(statement, name) == expected for name in (repo, project)):
            markers = [field.strip() for field in statement.split(";")[1:]
                       if field.strip().startswith("MIGRATION ")]
            if markers == [marker]:
                return
    raise MergeBatchError(refusal)


def _merge_subject(number, title, custom):
    """Return a single-line merge commit subject ending ``(#N)``, or raise."""
    suffix = f"(#{number})"
    if custom is not None:
        if "\n" in custom or "\r" in custom:
            raise MergeBatchError(f"merge subject must be a single line ending with '{suffix}'")
        text = custom.strip()
        if not re.search(rf"\S.*{re.escape(suffix)}\Z", text):
            raise MergeBatchError(f"merge subject must end with '{suffix}'")
        return text
    base = re.sub(r"[\r\n]+", " ", (title or "")).strip() or f"batch #{number}"
    return base if base.endswith(suffix) else f"{base} {suffix}"


def settle_train(train, lease, st=None, *, wait=0, allow_open=False):
    """Verify a pending landing, write its records, and resume the fenced settlement cursor."""
    repo, number = train["repo"], train["pr"]
    train = landing_journal(repo, "show", pr=number)["train"]
    if train["abandoned"] or (train["merge"] and not train["open_steps"]):
        return
    merge, state = read_verified_merge(train)
    if state == "closed" and not train["merge"]:
        landing_journal(repo, "abandon", pr=number, **lease)
        return
    if state == "open" and allow_open and not train["merge"]:
        return
    if not merge:
        raise MergeBatchError(f"train #{number} is {state}; settlement remains pending")
    if train["merge"] not in (None, merge):
        raise MergeBatchError("verified merge differs from the landing journal")
    if "included" not in train:
        checked = check_landed_parts(repo, number, train["parts"], merge, wait, report=False)
        train = landing_journal(repo, "landed", pr=number, merge=merge,
                                included=[part["pr"] for part in checked], **lease)
        if warning := train.get("dependency_warning"):
            print(f"train #{number} settled with an unmet dependency: {warning}", file=sys.stderr)
    if st is not None and st.get("head") != train["head"]:
        st = None
    attempt = frozen_landing(st) if st is not None else {}
    mode = train.get("mode") or attempt.get("mode") or "train-pr-merge"
    if "receipt_inputs" in train:
        write_landing_records(train, mode, merge, train["receipt_inputs"])
    elif st is not None:
        write_landing(st, mode, merge, attempt.get("reruns", []), attempt.get("go_row"))
    elif mode == TESTED_FF:
        raise MergeBatchError(f"train #{number} is missing receipt inputs; restore its matching local state to settle")
    landed = [part for part in train["parts"] if part["pr"] in train["included"]]
    for step in train["open_steps"]:
        receipt = {}
        if step == "evidence":
            for part in landed:
                print(f"MERGED {part['pr']} {merge} {','.join(part['items']) or '-'}")
        elif step == "close_parts":
            if mode == TESTED_FF and not seats_acknowledged():
                raise MergeBatchError(f"train #{number} settlement requires PG_MERGE_CLOSE_REQUIRE_SEATS=off (or false, 0)")
            result = run(["plangraph", "merge-close"], timeout=330)
            sys.stderr.write(result.stdout + result.stderr)
        elif step == "host_sync":
            started = landing_now()
            result = run(["plangraph", "landing", "verify-hosts", repo, merge], timeout=600)
            receipt["verification"] = json.loads(result.stdout)["verified"]
            sys.stderr.write(result.stdout)
            record_stage(repo, train["batch_key"], "host_sync", merge, started)
        elif step == "cleanup":
            stopped = writers_stopped(landing_journal(repo, "show", pr=number)["train"].get("host_verification"))
            if st is not None:
                cleanup_landed(st, landed, strict=True, writers=stopped)
                drop_state(st)
            else:
                parts = [part for part in landed if part["ref"]]
                state = {"repo": repo, "head": train["head"], "worktree": "", "branch": "",
                         "prs": {str(part["pr"]): part for part in parts}}
                cleanup_landed(state, parts, strict=True, writers=stopped)
        else:
            raise MergeBatchError(f"unknown keeper landing step {step}")
        landing_journal(repo, "advance", pr=number, merge=merge, step=step, **receipt, **lease)


def cmd_settle(args):
    from merge_batch_util import repo_info

    repo, base = repo_info()
    if base != "main":
        raise MergeBatchError("settlement requires the main repository checkout")
    states = {state.get("pr"): state for state in all_states()
              if state.get("repo", "").lower() == repo.lower()}
    pending = landing_journal(repo, "pending")["trains"]
    failures = []
    for train in pending:
        try:
            with landing_lease(repo) as lease:
                settle_train(train, lease, states.get(train["pr"]), allow_open=True)
        except MergeBatchError as exc:
            failures.append(str(exc))
    if failures:
        raise MergeBatchError("; ".join(failures))
    return 0


def cmd_land(args):
    st = load_state(args.name)
    mode = frozen_landing(st).get("mode") or landing_mode_of(st)
    if keeper_landing_configured():
        with landing_lease(st["repo"]) as lease:
            return land_with_lease(args, lease, st, mode)
    return land_with_lease(args, None, st, mode)


def land_with_lease(args, lease, st, mode):
    repo, number = st["repo"], args.batch_pr
    if st["status"] != "pushed" or st.get("pr") != number:
        raise MergeBatchError(f"batch {st['name']!r} is {st['status']} with batch PR {st.get('pr')}: "
                              f"push it first and pass its PR number (got #{number})")
    custom_subject = getattr(args, "subject", None)
    if custom_subject is not None:
        _merge_subject(number, None, custom_subject)
    check_approval(args.approval, st["head"])
    st = active_train(st, st["head"])
    if mode == "single-pr" and len(st["merged"]) != 1:
        raise MergeBatchError(f"single-pr mode lands one pull request on its own GO: batch {st['name']!r} has "
                              f"{len(st['merged'])} parts (set landing.mode to tested-ff or train-pr-merge)")
    require_part_seats(st)
    fields = "state,headRefOid,isDraft,mergeCommit,baseRefName,title"
    pr = gh_json("pr", "view", str(number), "--repo", repo, "--json", fields)
    if pr["headRefOid"] != st["head"]:
        raise MergeBatchError(f"batch PR #{number} head moved: {short(pr['headRefOid'])} on GitHub, "
                              f"{short(st['head'])} saved at push; review and push again")
    train = None
    reruns = []
    already = mode == TESTED_FF and in_history(st, st["head"], main_tip(st))
    if pr["state"] == "OPEN" and not already:
        if pr["isDraft"]:
            raise MergeBatchError(f"batch PR #{number} is a draft")
        if pr.get("baseRefName") != "main":
            raise MergeBatchError("full_ci: batch PR must target main")
        require_migration_go(st, args.go_row)
        batch_key = landing_batch_key(st) if lease else None
        reruns = require_full_run(repo, st["head"], st["branch"], batch_key)
        require_current_base(repo, st["head"])
        require_green(repo, number, [c for c in (args.checks or "").split(",") if c])
        record_reruns(repo, number, reruns)
        if mode == TESTED_FF:
            require_tested_ff_evidence(st)
        if lease:
            main = gh_json("api", f"repos/{repo}/git/ref/heads/main")
            if (main.get("object") or {}).get("sha") != st["base_head"]:
                raise MergeBatchError("stale_base: main moved from the frozen landing base")
            st["landing_attempt"] = dict(head=st["head"], mode=mode, reruns=reruns, go_row=args.go_row)
            save_state(st)
            train = prepare_train(st, lease, mode)
            if st.get("go_requested_head") == st["head"]:
                record_stage(repo, batch_key, "go", st["head"], st["go_requested_at_ms"])
        hold_lane_refs([st["prs"][str(part["pr"])].get("ref") for part in st["merged"]])  # held from before the merge until writer-stop
        started = landing_now()
        if mode == TESTED_FF:
            if lease:
                landing_journal(repo, "check_dependencies", pr=number, head=st["head"], **lease)
            push_tested(st, args.go_row)
        else:
            subject = _merge_subject(number, pr.get("title") or f"batch {st['name']}", custom_subject)
            require_main_green(repo, st["head"], args.go_row)
            if lease:
                landing_journal(repo, "check_dependencies", pr=number, head=st["head"], **lease)
            gh("pr", "merge", str(number), "--repo", repo, "--merge", "--match-head-commit", st["head"],
               "--subject", subject, "--body", merge_trailers(st))
        if lease:
            record_stage(repo, batch_key, "merge", st["head"], started)
    elif pr["state"] not in ("MERGED", "OPEN"):
        raise MergeBatchError(f"batch PR #{number} is {pr['state']}")
    if mode == TESTED_FF:
        if not in_history(st, st["head"], main_tip(st)):  # a MERGED pull request proves nothing: read the branch
            raise MergeBatchError(f"batch PR #{number} is {pr['state']} but its head {short(st['head'])} is not in "
                                  f"{st['base']}: it was merged another way, so this is no tested-ff landing")
        merge = st["head"]  # the tested commit itself is on main
        wait_marked_merged(repo, number, args.wait)
    else:
        for _ in range(10):  # the merge commit can lag the merge by a moment
            pr = gh_json("pr", "view", str(number), "--repo", repo, "--json", fields)
            if pr["state"] == "MERGED" and (pr.get("mergeCommit") or {}).get("oid"):
                break
            time.sleep(min(2.0, args.wait / 5))
        merge = (pr.get("mergeCommit") or {}).get("oid")
        if pr["state"] != "MERGED" or not merge:
            raise MergeBatchError(f"batch PR #{number} is {pr['state']} with no merge commit yet: run land again")
    for skip in st["skipped"]:
        print(f"not merged: #{skip['pr']} ({skip['reason']})", file=sys.stderr)
    if lease:
        if train is None:
            train = landing_journal(repo, "show", pr=number)["train"] or prepare_train(st, lease, mode)
        if train["head"] != st["head"] or train.get("mode", mode) != mode:
            raise MergeBatchError("keeper landing journal names a different attempt")
        settle_train(train, lease, st, wait=args.wait)
        return 0
    git("fetch", "--quiet", REMOTE, f"+refs/heads/{st['base']}:refs/remotes/{REMOTE}/{st['base']}", ok=None)
    landed = check_landed_parts(repo, number, st["merged"], merge, args.wait)
    write_landing(st, mode, merge, reruns, args.go_row)
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
    runs = [f"Review-Run: {key} {run} {rec['head']}" for rec in st["merged"]
            for key, run in sorted((rec.get("review_runs") or {}).items())]
    return "\n".join([f"Plan-Item: {key}" for key in train_keys(st)] + runs)


def cmd_status(args):
    states = all_states()
    if keeper_landing_configured():
        for state in states:
            if state.get("repo"):
                state["timeline"] = landing_report(state)
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
        if (st.get("timeline") or {}).get("summary"):
            print("  timeline: " + st["timeline"]["summary"])
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


def part_seat(run_id, key, head):
    from plangraph.keeper.config import ConfigError, load_config
    from plangraph.keeper.evidence_runs import RunRecordAdapter
    from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
    from plangraph.keeper.store_merge_locks import require_train_review

    if not isinstance(run_id, str) or not run_id.startswith("run-") or not RunRecordAdapter(None).valid_run_id(run_id):
        raise MergeBatchError("a keeper-bound review requires a spawner run id")
    try:
        config = load_config(keeper_config_path())
        adapter = SpawnerRecordAdapter(config.spawner_records_dir)
        require_train_review(adapter, key, head, run_id)
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
    pushed = st.get("status") == "pushed"
    head = st["head"] if pushed else git("rev-parse", "HEAD", cwd=st["worktree"]).stdout.strip()
    active = active_train(st, head)
    rec = next((r for r in active["merged"] if r["pr"] == args.pr), None)
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
    if pushed and keeper_landing_configured():
        with landing_lease(st["repo"]) as lease:
            prepare_train(active, lease)
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
    active = active_train(st, head)
    parts = []
    seats = require_part_seats(active)
    for rec in active["merged"]:
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
    version = keeper_migration(st)
    migration = f"MIGRATION {version}; " if version is not None else ""
    summary = landing_report(st).get("summary") if keeper_landing_configured() else None
    timeline = f"timeline: {summary}; " if summary else ""
    line = (f"GO-REQ #{st['pr']} {st['repo']} head {head}; diff stat: {stat}; parts: " + "; ".join(parts) +
            f"; CI run {ci['id']} state={ci['state']} conclusion={ci['conclusion']}; "
            f"{go_key_block(active)}; {timeline}{migration}PLAN {plan} conforms={'no' if deviations else 'yes'} deviations={deviation}"
            + (f"; {windows_report(st, head)}" if windows_report(st, head) else ""))
    if keeper_landing_configured() and st.get("go_requested_head") != head:
        st.update(go_requested_head=head, go_requested_at_ms=landing_now())
        save_state(st)
    print(" ".join(line.split()))
    return 0
