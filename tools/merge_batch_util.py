"""Plumbing for tools/merge_batch.py: git/gh subprocess wrappers, batch state files, PR records."""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from preflight import run_test_process

REMOTE = "origin"
BATCH_READY = "batch-ready"  # the label that marks a PR ready to join a batch, draft or not
NEEDS_UPDATE = "needs-update"
TESTED_FF = "tested-ff"  # the landing mode that moves main to the tested head (plangraph.settings.LANDING_MODES)
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


def part_items(st, rec, *, require_keys=True):
    wt, merge, head = st["worktree"], rec["merge"], rec["head"]
    if git("rev-parse", f"{merge}^2", cwd=wt).stdout.strip() != head:
        raise MergeBatchError(f"part #{rec['pr']} merge does not bind its head")
    from plangraph.keeper.evidence_git import plan_items  # the keeper's reading of a commit's trailers

    messages = git("log", "--format=%B%x1d", f"{merge}^1..{head}", cwd=wt).stdout.split("\x1d")
    keys = {key for message in messages for key in plan_items(message)}
    if (require_keys and not keys) or any(not re.fullmatch(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+", key) for key in keys):
        raise MergeBatchError(f"part #{rec['pr']} needs a Plan-Item for its keeper-bound review")
    return keys


def active_train(st, head):
    """The part table at this exact head, with skipped and reverted parts removed."""
    from plangraph.keeper.evidence_git import REVERT_GREP, delivery_revert

    base = st.get("base_head")
    if not base:
        raise MergeBatchError("batch has no saved base head for train evidence; rebuild the batch")
    skipped = {rec["pr"] for rec in st.get("skipped", [])}
    dropped = set()
    log = git("log", "--first-parent", "-E", "-i", "--grep", REVERT_GREP, "--format=%B%x1d",
              f"{base}..{head}", cwd=st["worktree"])
    for message in log.stdout.split("\x1d"):
        revert = delivery_revert(message.lstrip("\n"))
        if revert is None:
            continue
        target, keys = revert
        for part in st["merged"]:
            part_target = target and (part["merge"].startswith(target) or part["head"].startswith(target))
            if part_target or keys.intersection(part["items"]):
                dropped.add(part["pr"])
    return dict(st, merged=[rec for rec in st["merged"] if rec["pr"] not in skipped | dropped],
                dropped=[rec for rec in st["merged"] if rec["pr"] in dropped])


def train_key_lists(st):
    """``(delivered, deferred)``: the Plan-Item trailers of the train's commits not on main (resolver
    commits and extra slices count; checkpoint-doc-only keys are deferred; dropped parts' keys go)."""
    from plangraph.keeper.evidence_git import GitAdapter
    from plangraph.keeper.evidence_pr import train_trailer_keys

    kept = {key for rec in st["merged"] for key in part_items(st, rec, require_keys=False)}
    delivered, deferred = set(kept), set()
    base, head, wt = st.get("base_head"), st.get("head"), st.get("worktree")
    if base and head and wt:
        origin = f"origin/{st.get('base') or 'main'}"
        known = git("rev-parse", "--verify", "-q", f"{origin}^{{commit}}", cwd=wt, ok=(0, 1)).returncode == 0
        found, held = train_trailer_keys(GitAdapter(), wt, head, [base, *([origin] if known else [])])
        gone = {key for rec in [*st.get("dropped", []), *st.get("skipped", [])] for key in rec.get("items", [])} - kept
        delivered = (delivered | set(found)) - gone - set(held)
        deferred = set(held) - gone
    if re.fullmatch(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+", st.get("plan") or ""):
        delivered.add(st["plan"])
    return sorted(delivered), sorted(deferred)


def train_keys(st):
    return train_key_lists(st)[0]


def go_key_block(st):
    delivered, deferred = train_key_lists(st)
    return "GO-KEYS [" + ",".join(delivered) + "]" + ("; GO-DEFERRED [" + ",".join(deferred) + "]" if deferred else "")


def keeper_migration(st):
    """Read migration versions from the frozen Git trees without executing branch code."""
    path = "src/plangraph/keeper/schema_pg.py"
    base, head, cwd = st.get("base_head"), st["head"], st["worktree"]
    if not base:
        raise MergeBatchError("batch has no saved base head for keeper migration detection; rebuild the batch")
    if not git("diff", "--name-only", base, head, "--", path, cwd=cwd).stdout:
        return None

    def latest(rev):
        if not git("ls-tree", rev, "--", path, cwd=cwd).stdout:
            return 0
        try:
            tree = ast.parse(git("show", f"{rev}:{path}", cwd=cwd).stdout)
            registry, = [node.value for node in tree.body if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "MIGRATIONS"
                                 for target in node.targets)]
            versions = [entry.args[0].value for entry in registry.elts
                        if isinstance(entry, ast.Call) and isinstance(entry.func, ast.Name)
                        and entry.func.id == "Migration"]
            if (not versions or len(versions) != len(registry.elts)
                    or any(type(version) is not int for version in versions)
                    or versions != list(range(1, len(versions) + 1))):
                raise ValueError("migration versions must be consecutive from 1")
            return versions[-1]
        except (SyntaxError, AttributeError, IndexError, ValueError) as exc:
            raise MergeBatchError(f"keeper migration version is unreadable at {rev}: {exc}") from None

    previous, current = latest(base), latest(head)
    if current < previous:
        raise MergeBatchError(f"keeper migration regressed from {previous} to {current}")
    return current if current > previous else None


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


# --- landing telemetry (LAND-MODE-MEASURE): one row per landing, the report compares the modes ------
#: Columns a landing row takes, besides the key (repo, train, landed_sha). A time is an ISO-8601 string.
LANDING_COLUMNS = ("mode", "pr", "parts", "first_ready_at", "build_started_at", "ci_run_id", "ci_started_at",
                   "ci_ended_at", "go_at", "landed_at", "green_at", "ci_reruns", "fit_commits", "left_out",
                   "red_main", "fix_tasks")
LANDING_SCHEMA = """CREATE TABLE IF NOT EXISTS landing (
    repo TEXT NOT NULL, train TEXT NOT NULL, landed_sha TEXT NOT NULL, mode TEXT NOT NULL, pr INTEGER,
    parts TEXT NOT NULL, first_ready_at TEXT, build_started_at TEXT, ci_run_id INTEGER, ci_started_at TEXT,
    ci_ended_at TEXT, go_at TEXT, landed_at TEXT, green_at TEXT, ci_reruns INTEGER, fit_commits INTEGER,
    left_out INTEGER, red_main INTEGER, fix_tasks INTEGER, PRIMARY KEY (repo, landed_sha))"""


def landing_modes():
    """The landing modes of the setting (plangraph.settings.LANDING_MODES, the one list)."""
    from plangraph.settings import LANDING_MODES

    return LANDING_MODES


def landing_db_path():
    """The telemetry database file: $PLANGRAPH_LANDING_DB, else plangraph/landing-telemetry.sqlite3 under
    $XDG_DATA_HOME (default ~/.local/share)."""
    explicit = os.environ.get("PLANGRAPH_LANDING_DB")
    if explicit:
        return Path(explicit)
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "plangraph" / "landing-telemetry.sqlite3"


def landing_db():
    """The open telemetry database. A missing table is created (one write transaction, the check repeated under
    the lock); a current file is opened without any write lock, so a report runs beside the lander. A file whose
    table is keyed by train (written by an earlier draft) is refused and left untouched: its rows say nothing
    about which writer of one landing is authoritative, so nothing is guessed; move the file aside."""
    path = landing_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row

    def table():
        return list(db.execute("PRAGMA table_info(landing)"))

    if not table():
        db.execute("BEGIN IMMEDIATE")
        try:
            if not table():
                db.execute(LANDING_SCHEMA)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    if "train" in [c["name"] for c in table() if c["pk"]]:
        db.close()
        raise MergeBatchError(f"landing telemetry {path} is keyed by train (an earlier draft): move it aside, "
                              "a landing is now identified by repository and landed sha")
    db.isolation_level = ""  # the callers use ``with db`` transactions
    return db


def record_landing(row, fill_only=False):
    """Write the one telemetry row of a landing, identified by (repo, landed_sha); a second write for it adds
    what the first did not know and never blanks a known value. ``fill_only`` (a backfill) fills only the columns
    that are still unknown and never changes a recorded mode, train or part list: a live row stays authoritative.

    ``row`` needs repo, train, landed_sha and a mode of LANDING_MODES; ``parts`` is a list of PR numbers.
    A column the lander cannot know yet stays NULL (``landing-settle`` fills the green and red-main
    columns later). An unknown mode is refused, so a row never names a path that does not exist."""
    if row.get("mode") not in landing_modes():
        raise MergeBatchError(f"landing telemetry: unknown mode {row.get('mode')!r}")
    for key in ("repo", "train", "landed_sha"):
        if not row.get(key):
            raise MergeBatchError(f"landing telemetry: row has no {key}")
    values = {name: row.get(name) for name in LANDING_COLUMNS} | {"train": row["train"]}
    values["parts"] = json.dumps(sorted(row.get("parts") or [], key=str))
    columns = ("train", *LANDING_COLUMNS)
    with landing_db() as db:
        db.execute("INSERT OR IGNORE INTO landing (repo, landed_sha, train, mode, parts) VALUES (?, ?, ?, ?, ?)",
                   (row["repo"], row["landed_sha"], row["train"], row["mode"], values["parts"]))
        # a landing is identified by (repo, landed sha): a later writer (backfill after the lander, or the reverse)
        # fills what it knows and never blanks what an earlier one recorded
        known = {k: v for k, v in values.items() if k in columns and v is not None}
        if fill_only:
            known = {k: v for k, v in known.items() if k not in ("train", "mode")}
            assign = ", ".join(f"{k}=COALESCE({k}, ?)" for k in known)
        else:
            assign = ", ".join(f"{k}=?" for k in known)
        db.execute(f"UPDATE landing SET {assign} WHERE repo=? AND landed_sha=?",
                   [*known.values(), row["repo"], row["landed_sha"]])


def landing_rows():
    """Every telemetry row, oldest landing first, with ``parts`` as a list."""
    with landing_db() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM landing ORDER BY landed_at, train")]
    for r in rows:
        r["parts"] = json.loads(r["parts"])
    return rows


def require_landing_row(repo, train, sha, mode):
    """The lander's own check: the landing has its telemetry row, naming the mode it landed in."""
    with landing_db() as db:
        found = db.execute("SELECT mode FROM landing WHERE repo=? AND landed_sha=?", (repo, sha)).fetchone()
    if found is None or found["mode"] != mode:
        raise MergeBatchError(f"landing of {train} at {short(sha)} has no telemetry row for mode {mode}: "
                              "the landing is not counted (landing-report would miss it)")


def minutes(start, end):
    """Minutes from one ISO-8601 time to another (negative when end is first); None when either is unknown."""
    try:
        first, last = (datetime.fromisoformat(str(t).replace("Z", "+00:00")) for t in (start, end))
    except (TypeError, ValueError):
        return None
    return (last - first).total_seconds() / 60


def _spread(values):
    values = [v for v in values if v is not None]
    return {"median": float(statistics.median(values)), "worst": float(max(values))} if values else None


def landing_report(rows=None):
    """One entry per landing mode, comparing the modes: trains, median and worst minutes from the first part
    READY to landed and from landed to the first green run, mean CI test minutes per landed part, the count
    of trains whose main went red, and the mean fix tasks per train that has the count. A value that is not
    known for a train stays out of its statistic; a statistic with no value is None."""
    rows = landing_rows() if rows is None else rows
    report = []
    for mode in landing_modes():
        mine = [r for r in rows if r["mode"] == mode]
        if not mine:
            continue
        test = [minutes(r["ci_started_at"], r["ci_ended_at"]) for r in mine]
        per_part = [m / len(r["parts"]) for m, r in zip(test, mine) if m is not None and r["parts"]]
        fixes = [r["fix_tasks"] for r in mine if r["fix_tasks"] is not None]
        report.append({
            "mode": mode, "trains": len(mine), "parts": sum(len(r["parts"]) for r in mine),
            "ready_to_landed_min": _spread([minutes(r["first_ready_at"], r["landed_at"]) for r in mine]),
            "landed_to_green_min": _spread([minutes(r["landed_at"], r["green_at"]) for r in mine]),
            "test_min_per_part": sum(per_part) / len(per_part) if per_part else None,
            "ci_reruns": sum(r["ci_reruns"] or 0 for r in mine),
            "red_main": sum(1 for r in mine if r["red_main"]),
            "fix_tasks_per_train": sum(fixes) / len(fixes) if fixes else None})
    return report


def render_landing_report(report):
    """The comparison of landing_report as a text table, one line per mode."""
    def spread(value):
        return "-" if value is None else f"{value['median']:.0f}/{value['worst']:.0f}"

    def number(value):
        return "-" if value is None else f"{value:.1f}"

    lines = ["mode            trains parts ready->landed(med/worst min) landed->green(med/worst min) "
             "test min/part reruns red main fix/train"]
    for r in report:
        lines.append(f"{r['mode']:<15} {r['trains']:>6} {r['parts']:>5} {spread(r['ready_to_landed_min']):>25} "
                     f"{spread(r['landed_to_green_min']):>26} {number(r['test_min_per_part']):>13} "
                     f"{r['ci_reruns']:>6} {r['red_main']:>8} {number(r['fix_tasks_per_train']):>9}")
    return "\n".join(lines) + "\n"


def settle_landings(fetch_runs):
    """Fill the green and red-main columns of landings from the runs on the landed sha.

    ``fetch_runs(repo, sha)`` returns the workflow runs (dicts with status, conclusion, updated_at) on that
    sha. A completed failure or timeout marks the row red_main=1; with none and a completed run, red_main=0;
    the first completed success sets green_at when it is unknown. A row with no completed run is left as
    it is. Returns the number of rows changed."""
    changed = 0
    with landing_db() as db:
        pending = db.execute("SELECT repo, train, landed_sha, green_at, red_main FROM landing "
                             "WHERE green_at IS NULL OR red_main IS NULL").fetchall()
        for found in pending:
            done = [r for r in fetch_runs(found["repo"], found["landed_sha"]) if r.get("status") == "completed"]
            wins = sorted(r["updated_at"] for r in done if r.get("conclusion") == "success")
            red = any(r.get("conclusion") in ("failure", "timed_out") for r in done)
            if not done:
                continue
            green_at = found["green_at"] or (wins[0] if wins else None)
            red_main = 1 if red else 0
            if (green_at, red_main) != (found["green_at"], found["red_main"]):
                db.execute("UPDATE landing SET green_at=?, red_main=? WHERE repo=? AND landed_sha=?",
                           (green_at, red_main, found["repo"], found["landed_sha"]))
                changed += 1
    return changed


def set_fix_tasks(train, count):
    """Record how many fix tasks named ``train`` on every telemetry row of that train."""
    with landing_db() as db:
        changed = db.execute("UPDATE landing SET fix_tasks=? WHERE train=?", (count, train)).rowcount
    if not changed:
        raise MergeBatchError(f"no telemetry row for train {train!r}")


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ci_window(repo, head):
    """Run id and times of the full tests run on ``head`` (empty when GitHub does not say)."""
    try:
        runs = gh_json("api", f"repos/{repo}/actions/workflows/tests.yml/runs?head_sha={head}"
                              "&event=pull_request&per_page=100")["workflow_runs"]
        run_ = max(runs, key=lambda r: r["id"])
        return {"ci_run_id": run_["id"], "ci_started_at": run_.get("run_started_at") or run_.get("created_at"),
                "ci_ended_at": run_.get("updated_at"), "ci_reruns": len(runs) - 1 + run_.get("run_attempt", 1) - 1}
    except (MergeBatchError, KeyError, ValueError, TypeError):
        return {}


def first_ready(repo, recs):
    """The earliest time a part got the batch-ready label (None when GitHub does not say)."""
    times = []
    for rec in recs:
        try:
            events = gh_json("api", f"repos/{repo}/issues/{rec['pr']}/events?per_page=100")
            times += [e["created_at"] for e in events
                      if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == "batch-ready"]
        except (MergeBatchError, KeyError, ValueError, TypeError):
            continue
    return min(times) if times else None


def landing_receipt_inputs(st, reruns, go_row):
    """Freeze the receipt inputs that cannot be reconstructed from the train and GitHub."""
    go_path = Path(go_row) if go_row else None
    go_at = (datetime.fromtimestamp(go_path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
             if go_path and go_path.is_file() else st.get("go_at"))
    return {"train": st["name"], "build_started_at": st.get("created"), "go_at": go_at,
            "ci_reruns": len(reruns), "fit_commits": sum(bool(r["resolved"]) for r in st["merged"]),
            "left_out": len(st["skipped"]), "plan_item": st.get("plan"),
            "review_runs": {str(r["pr"]): r.get("review_runs") or {} for r in st["merged"]}}


def write_landing_records(train, mode, merge, inputs):
    """Write and verify the telemetry row, then write its receipt for the verified landing."""
    repo, name = train["repo"], check_name(inputs["train"])
    parts, landed_at = train["parts"], train["landed_at"]
    row = {"repo": repo, "mode": mode, "train": name, "pr": train["pr"], "parts": [r["pr"] for r in parts],
           "landed_sha": merge, "first_ready_at": first_ready(repo, parts), "build_started_at": inputs["build_started_at"],
           "go_at": inputs["go_at"] or landed_at, "landed_at": landed_at, "ci_reruns": inputs["ci_reruns"],
           "fit_commits": inputs["fit_commits"], "left_out": inputs["left_out"]}
    window = ci_window(repo, train["head"])
    window.pop("ci_reruns", None)  # use the reruns recorded when the train was admitted
    row.update(window)
    receipt = {key: inputs[key] for key in ("plan_item", "review_runs")}
    if mode == TESTED_FF:
        row["green_at"] = row.get("ci_ended_at")  # the green run is the run on the landed commit itself
    try:
        record_landing(row)
        require_landing_row(repo, name, merge, mode)
        receipts = state_dir() / "receipts"
        receipts.mkdir(parents=True, exist_ok=True)
        (receipts / f"{name}-{merge}.json").write_text(json.dumps({**row, **receipt}, indent=2, sort_keys=True) + "\n",
                                                     encoding="utf-8")
    except (sqlite3.Error, OSError) as exc:
        raise MergeBatchError(f"landing telemetry row not written: {exc}") from None
    print(f"landing receipt: mode={mode} train={name} sha={merge}", file=sys.stderr)


def write_landing(st, mode, merge, reruns, go_row):
    """Write the landing records from local state and save the timestamp for retries."""
    st.setdefault("landed_at", iso_now())
    write_landing_records(dict(st, parts=st["merged"]), mode, merge, landing_receipt_inputs(st, reruns, go_row))
    save_state(st)


def pr_labels(st):
    """The labels CI sees on the batch pull request (none while it does not exist yet)."""
    if not st.get("pr"):
        return []
    try:
        view = gh_json("pr", "view", str(st["pr"]), "--repo", st["repo"], "--json", "labels")
        return [label["name"] for label in view.get("labels", [])]
    except (MergeBatchError, KeyError, TypeError):
        return []


def landing_now():
    return time.time_ns() // 1_000_000


def landing_batch_key(st):
    return st.get("plan") or st["name"]


def keeper_landing_configured():
    default_env = Path.home() / ".config" / "plangraph" / "plangraph.env"
    return bool(os.environ.get("PG_URL") or os.environ.get("PLANGRAPH_ENV_FILE") or default_env.is_file())


def close_merged_items():
    """Run the keeper merge-close job once for the landed batch; its report goes to stderr, never fails the land."""
    try:
        proc = run(["plangraph", "merge-close"], ok=None, timeout=330)
    except MergeBatchError as exc:
        print(f"merge-close not run: {exc}", file=sys.stderr)
        return
    sys.stderr.write(proc.stdout + proc.stderr)


def cleanup_landed(st, landed, *, strict=False):
    """Best effort cleanup with exact state SHAs; all sweep protections still apply."""
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
        argv = ["pg-worktree-sweep", "--repo", repo, "--pins", pins, "--apply", "--delete-remote", "--include-merged"]
        roots = dict.fromkeys(Path(path).parent for path, _, _ in targets)
        for key in ("PG_WORKTREES", "PG_CLAUDE_WORKTREES", "PG_RUNS", "PG_RUN_WORKTREES"):
            if os.environ.get(key):
                roots[Path(os.environ[key])] = None
        for root in roots or (repo,):
            argv.extend(["--root", str(root)])
        for path, branch, sha in targets:
            argv.extend(["--target", f"{path}:{'refs/heads/' + branch if branch else '-'}:{sha}"])
        for rec in landed:
            branch = st["prs"][str(rec["pr"])]["ref"]
            argv.extend(["--remote-target", f"{branch}:{rec['head']}"])
        result = run(argv)
        print(result.stdout, end="", file=sys.stderr)
    except MergeBatchError as exc:
        if strict:
            raise
        print(f"cleanup sweep failed: {exc}", file=sys.stderr)
