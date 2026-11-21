"""Plumbing for tools/merge_batch.py: git/gh subprocess wrappers, batch state files, PR records."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from preflight import run_test_process

REMOTE = "origin"
BATCH_READY = "batch-ready"  # the label that marks a PR ready to join a batch, draft or not
NEEDS_UPDATE = "needs-update"
EXIT_REFUSED, EXIT_USAGE, EXIT_CONFLICT = 1, 2, 3
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
ITEM_RE = re.compile(r"^Plan-Item:[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)
#: gh --json fields of every PR lookup (all exist for `gh pr list` and `gh pr view`).
PR_FIELDS = ("number,title,headRefName,headRefOid,baseRefName,state,isDraft,isCrossRepository,"
             "labels,files,changedFiles,additions,deletions,statusCheckRollup")


class MergeBatchError(Exception):
    """A refusal or failure; the message goes to the operator (exit 1)."""


def first_line(text):
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[0] if lines else "no output"


def run(argv, cwd=None, ok=(0,), timeout=600, progress=False, input=None):
    """Run argv; a return code outside ``ok`` raises (``ok=None`` accepts any), as does a timeout."""
    try:
        proc = run_test_process(argv, cwd=cwd, input=input, stdout=sys.stderr if progress else subprocess.PIPE,
                                stderr=None if progress else subprocess.PIPE,
                                text=True, errors="replace", timeout=timeout)
    except OSError as exc:
        raise MergeBatchError(f"cannot run {argv[0]}: {exc.strerror or exc}") from None
    except subprocess.TimeoutExpired:
        raise MergeBatchError(f"{' '.join(argv[:3])} did not finish in {timeout}s") from None
    if ok is not None and proc.returncode not in ok:
        raise MergeBatchError(f"{' '.join(argv[:3])} failed (rc {proc.returncode}): "
                              f"{first_line(proc.stderr or proc.stdout)}")
    return proc


def git(*args, cwd=None, ok=(0,)):
    if args and args[0] == "push":
        # The hook owns per-step deadlines, including multiple Rust commands.
        # Forward git/hook output to stderr so long checks show progress.
        return run(["git", *args], cwd=cwd, ok=ok, timeout=None, progress=True)
    return run(["git", *args], cwd=cwd, ok=ok)


def gh(*args, ok=(0,)):
    return run(["gh", *args], ok=ok)


def gh_json(*args, ok=(0,)):
    proc = gh(*args, ok=ok)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise MergeBatchError(f"gh {' '.join(args[:2])}: output is not JSON: "
                              f"{first_line(proc.stdout or proc.stderr)}") from None


def short(sha):
    return (sha or "")[:12]


def check_name(name):
    if not NAME_RE.fullmatch(name or ""):
        raise MergeBatchError(f"bad batch name {name!r}: use letters, digits, '_' and '-' "
                              "(at most 48, first a letter or digit)")
    return name


def parse_prs(text):
    nums = []
    for tok in re.split(r"[,\s]+", (text or "").strip()):
        if not tok:
            continue
        if not re.fullmatch(r"#?\d+", tok):
            raise MergeBatchError(f"bad PR number {tok!r} in --prs")
        if int(tok.lstrip("#")) not in nums:
            nums.append(int(tok.lstrip("#")))
    if not nums:
        raise MergeBatchError("--prs names no PR")
    return nums


def items_in(text):
    """Plan-Item values in commit message text, first-seen order, no repeats."""
    found = []
    for match in ITEM_RE.finditer(text or ""):
        if match.group(1) not in found:
            found.append(match.group(1))
    return found


# --- state: <git common dir>/merge-batch/<name>.json (same file from every worktree) ---------
def state_dir():
    out = git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(out) / "merge-batch"


def state_path(name):
    return state_dir() / f"{name}.json"


def load_state(name):
    path = state_path(check_name(name))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MergeBatchError(f"no batch {name!r} in progress (merge_batch.py status lists them)") from None
    except ValueError as exc:
        raise MergeBatchError(f"state file {path} is not valid JSON: {exc}") from None


def save_state(st):
    path = state_path(st["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{st['name']}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(st, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def all_states():
    out = []
    for path in sorted(state_dir().glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            out.append({"name": path.stem, "status": "unreadable", "merged": [], "skipped": [], "pending": []})
    return out


def drop_refs(name):
    """Delete the PR-head refs a build of batch ``name`` fetched."""
    for ref in git("for-each-ref", "--format=%(refname)", f"refs/merge-batch/{name}/").stdout.split():
        git("update-ref", "-d", ref)


def drop_state(st):
    """Delete the state file and the PR-head refs the build fetched."""
    drop_refs(st["name"])
    state_path(st["name"]).unlink(missing_ok=True)
    state_path(st["name"]).with_suffix(".attrs").unlink(missing_ok=True)


def orphan_names():
    """Batch names with PR-head refs (refs/merge-batch/NAME/N, made by a build) but no state file: what an
    old build left when it died before saving its state, or a state file deleted by hand."""
    names = {ref.split("/")[2] for ref in git("for-each-ref", "--format=%(refname)", "refs/merge-batch/").stdout.split()
             if ref.count("/") >= 3}
    return sorted(name for name in names if not state_path(name).exists())


def worktrees():
    """[(path, branch)] of every worktree of the repo; branch is '' for a detached one."""
    out, cur = [], {}
    for line in [*git("worktree", "list", "--porcelain").stdout.splitlines(), ""]:
        if line:
            key, _, value = line.partition(" ")
            cur[key] = value
        elif cur:
            out.append((cur.get("worktree", ""), cur.get("branch", "").removeprefix("refs/heads/")))
            cur = {}
    return out


# --- PR records -------------------------------------------------------------------------------
def ci_state(rollup):
    """red (any failed check) > pending (any unfinished) > green; none when nothing ran."""
    if not rollup:
        return "none"
    states = []
    for chk in rollup:
        if chk.get("__typename") == "StatusContext":
            state = (chk.get("state") or "").upper()
            states.append("pass" if state == "SUCCESS" else "pending" if state in ("PENDING", "EXPECTED") else "fail")
        elif (chk.get("status") or "").upper() != "COMPLETED":
            states.append("pending")
        else:
            ok = (chk.get("conclusion") or "").upper() in ("SUCCESS", "NEUTRAL", "SKIPPED")
            states.append("pass" if ok else "fail")
    return "red" if "fail" in states else "pending" if "pending" in states else "green"


def norm_pr(raw):
    if not SHA_RE.fullmatch(raw.get("headRefOid") or ""):
        raise MergeBatchError(f"PR #{raw.get('number')}: head {raw.get('headRefOid')!r} is not a commit sha")
    files = raw.get("files") or []
    lines = (raw.get("additions") or 0) + (raw.get("deletions") or 0)
    return {
        "number": raw["number"], "title": raw.get("title") or "", "head": raw["headRefOid"],
        "ref": raw["headRefName"], "base": raw["baseRefName"], "state": raw.get("state") or "OPEN",
        "draft": bool(raw.get("isDraft")), "cross": bool(raw.get("isCrossRepository")),
        "labels": [lab["name"] for lab in raw.get("labels") or []],
        "files": [f["path"] for f in files], "truncated": (raw.get("changedFiles") or 0) > len(files),
        "extra": [],  # old rename paths from Git (refresh_diff)
        "lines": lines or sum(
            (f.get("additions") or 0) + (f.get("deletions") or 0) for f in files),
        "ci": ci_state(raw.get("statusCheckRollup")),
    }


def fetch_pr(repo, number):
    return norm_pr(gh_json("pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS))


def refresh_diff(base, prs, heads=None):
    """Replace GitHub's stale/truncated diff metadata with the fetched Git diff.

    Keep both rename paths for HIGH checks. Generated churn (Cargo.lock, generated/
    trees and minified files) is reported separately but remains inside the cap.
    ``heads`` is supplied by build after fetching and checking the PR head refs.
    """
    prs = list(prs)
    if not prs:
        return
    if heads is None:
        refspecs = [f"+refs/heads/{base}:refs/remotes/{REMOTE}/{base}", *[f"refs/pull/{pr['number']}/head" for pr in prs]]
        try:
            git("fetch", "--quiet", "--no-write-fetch-head", REMOTE, *refspecs)
        except MergeBatchError as exc:
            raise MergeBatchError(f"cannot fetch the PR heads from {REMOTE} to read their paths: {exc} "
                                  f"(run it in a checkout whose {REMOTE} is the repo)") from None
        heads = {pr["number"]: pr["head"] for pr in prs}
    by_ref = {}
    for pr in sorted(prs, key=lambda pr: pr["number"]):
        if not pr["cross"]:
            by_ref.setdefault(pr["ref"], pr)
    for pr in prs:
        try:
            diff = f"{REMOTE}/{base}...{heads[pr['number']]}"
            paths = git("diff", "--name-only", "-z", diff).stdout
            all_paths = git("diff", "--name-only", "--no-renames", "-z", diff).stdout
            files = [path for path in paths.split("\0") if path]
            def generated(path):
                return (Path(path).name == "Cargo.lock" or "generated" in Path(path).parts
                        or ".min." in Path(path).name)

            def lines(comparison):
                stat = git("diff", "--shortstat", comparison).stdout
                # The first number is the file count; the others are added/deleted
                # lines. Avoid matching translated insertion/deletion labels.
                return sum(map(int, re.findall(r"\d+", stat)[1:]))

            generated_files, generated_lines = set(), 0
            records = iter(git("diff", "--numstat", "-z", diff).stdout.split("\0"))
            for record in records:
                if not record:
                    continue
                added, deleted, path = record.split("\t", 2)
                paths = [path] if path else [next(records), next(records)]  # rename: old, new
                if any(generated(path) for path in paths):
                    generated_files.update(path for path in paths if generated(path))
                    generated_lines += sum(int(n) for n in (added, deleted) if n != "-")
            parent = by_ref.get(pr["base"])
            # Display each diff against main, but count only this layer of a stack
            # toward the batch cap, as with GitHub's per-PR sizes before refresh.
            contribution = f"{heads[parent['number']]}...{heads[pr['number']]}" if parent else diff
            pr.update(files=files, truncated=False, lines=lines(diff), batch_lines=lines(contribution),
                      generated_lines=generated_lines, generated_files=sorted(generated_files),
                      extra=sorted(set(all_paths.split("\0")) - set(files) - {""}))
        except MergeBatchError as exc:
            raise MergeBatchError(f"cannot read the diff of PR #{pr['number']} ({short(pr['head'])}): {exc}") from None


def repo_info(repo=None):
    """(OWNER/NAME, default branch) from gh; ``repo`` names the repo, else gh's current one."""
    data = gh_json("repo", "view", *([repo] if repo else []), "--json", "nameWithOwner,defaultBranchRef")
    return data["nameWithOwner"], (data.get("defaultBranchRef") or {}).get("name")


def add_label(repo, number, label):
    """Label a PR through the REST API (gh 2.45's `pr edit` dies on Projects (classic)), creating
    the label once when the repo lacks it; False unless the returned list confirms it."""
    for attempt in (1, 2):
        done = gh("api", "--method", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={label}",
                  ok=None)
        if done.returncode == 0:
            try:
                labels = json.loads(done.stdout)
            except ValueError:
                return False
            if not isinstance(labels, list) or not all(
                    isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in labels):
                return False
            return any(entry["name"] == label for entry in labels)
        if attempt == 1:
            gh("label", "create", label, "--repo", repo, "--color", "d93f0b",
               "--description", "PR needs an update before it can join a merge batch", ok=None)
    return False


def update_pr(repo, number, title, body_path):
    """Set a PR's title and body (REST, for the same reason as add_label)."""
    gh("api", "--method", "PATCH", f"repos/{repo}/pulls/{number}", "-f", f"title={title}",
       "-F", f"body=@{body_path}")


def part_review(path, head):
    """Read an unambiguous final verdict bound to this part's full head."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise MergeBatchError(f"review file {path} cannot be read: {exc}") from None
    lines = [line.rstrip() for line in text.split("\n") if line.strip()]
    verdicts = re.findall(r"(?im)^VERDICT: ([A-Z_]+)\s*$", text)
    heads = re.findall(r"(?im)^HEAD: ([0-9a-f]{40})\s*$", text)
    verdict = verdicts[0] if len(verdicts) == 1 else ""
    if (verdict not in ("ACCEPT", "REQUEST_CHANGES", "REJECT") or len(lines) < 2 or
            lines[-1] != f"VERDICT: {verdict}" or lines[-2].lower() != f"head: {head}" or
            [h.lower() for h in heads] != [head]):
        raise MergeBatchError(f"review file {path} needs one HEAD: {head} followed by one "
                              "VERDICT: ACCEPT, REQUEST_CHANGES or REJECT at its end")
    if verdict == "ACCEPT" and re.search(
            r"(?i)verdict[\W_]*:[\W_]*(?:fail|fix|halt|reject|change|request[\W_]*change)", text):
        raise MergeBatchError(f"review file {path} has conflicting verdicts; cannot record VERDICT: ACCEPT")
    return {"file": str(Path(path).resolve()), "head": head, "verdict": verdict}



def workflow_gate(wt, base, head):
    """Every workflow path any commit of base..head edits, merges included, needs Gate-Edit on its commits."""
    import gate_wiring_check as gate

    try:
        shas = gate._commit_shas(base, head, wt, True)
        # The shared detector, not a path scan of the log: a conflict resolved by keeping one side
        # (or a rename conflict) shows nothing in the combined diff.
        paths = sorted({p for sha in shas for p in gate._commit_paths(sha, wt, True)
                        if p.startswith(".github/workflows/")})
    except gate.GateError as exc:
        raise MergeBatchError(f"workflow Gate-Edit check failed: {exc}") from None
    for path in paths:
        try:
            missing = gate.commits_missing_gate_edit(base, head, path, cwd=wt, include_merges=True)
        except gate.GateError as exc:
            raise MergeBatchError(f"workflow Gate-Edit check failed: {exc}") from None
        if missing:
            raise MergeBatchError(f"workflow {path} needs Gate-Edit: D-id on touching commit(s): " + " ".join(missing))


def batch_workflow_gate(st):
    workflow_gate(st["worktree"], st["base_head"], st["head"])
