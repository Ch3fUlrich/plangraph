"""Plumbing for tools/merge_batch.py: git/gh subprocess wrappers, batch state files, PR records."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

REMOTE = "origin"
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


def run(argv, cwd=None, ok=(0,), timeout=600):
    """Run argv; a return code outside ``ok`` raises (``ok=None`` accepts any), so does a hang."""
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except OSError as exc:
        raise MergeBatchError(f"cannot run {argv[0]}: {exc.strerror or exc}") from None
    except subprocess.TimeoutExpired:
        raise MergeBatchError(f"{' '.join(argv[:3])} did not finish in {timeout}s") from None
    if ok is not None and proc.returncode not in ok:
        raise MergeBatchError(f"{' '.join(argv[:3])} failed (rc {proc.returncode}): "
                              f"{first_line(proc.stderr or proc.stdout)}")
    return proc


def git(*args, cwd=None, ok=(0,)):
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


def drop_state(st):
    """Delete the state file and the PR-head refs the build fetched."""
    prefix = f"refs/merge-batch/{st['name']}/"
    for ref in git("for-each-ref", "--format=%(refname)", prefix).stdout.split():
        git("update-ref", "-d", ref)
    state_path(st["name"]).unlink(missing_ok=True)
    state_path(st["name"]).with_suffix(".attrs").unlink(missing_ok=True)


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
        "lines": lines or sum(
            (f.get("additions") or 0) + (f.get("deletions") or 0) for f in files),
        "ci": ci_state(raw.get("statusCheckRollup")),
    }


def fetch_pr(repo, number):
    return norm_pr(gh_json("pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS))


def repo_info(repo=None):
    """(OWNER/NAME, default branch) from gh; ``repo`` names the repo, else gh's current one."""
    data = gh_json("repo", "view", *([repo] if repo else []), "--json", "nameWithOwner,defaultBranchRef")
    return data["nameWithOwner"], (data.get("defaultBranchRef") or {}).get("name")


def add_label(repo, number, label):
    """Label a PR through the REST API (gh 2.45's `pr edit` dies on Projects (classic)), creating
    the label once when the repo lacks it; False when it cannot be added."""
    for attempt in (1, 2):
        done = gh("api", "--method", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={label}",
                  ok=None)
        if done.returncode == 0:
            return True
        if attempt == 1:
            gh("label", "create", label, "--repo", repo, "--color", "d93f0b",
               "--description", "PR needs an update before it can join a merge batch", ok=None)
    return False


def update_pr(repo, number, title, body_path):
    """Set a PR's title and body (REST, for the same reason as add_label)."""
    gh("api", "--method", "PATCH", f"repos/{repo}/pulls/{number}", "-f", f"title={title}",
       "-F", f"body=@{body_path}")
