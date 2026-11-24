"""``pg check`` (PLAN-HYGIENE): read-only checks over the local plan snapshot.

Findings, printed as one line each:

* ``CANCELLED-GATE-DEP <gate> -> <dep>`` — a gate/goal still depends on
  cancelled work; remove that dependency before adding children.
* ``ORDER-ONLY <src> -> <dst>`` — ``src`` waits on ``dst``, but nothing in
  ``dst``'s done criteria needs ``src``'s code and their declared ``paths``
  touch different trees, so the edge only orders the plan (it could be a
  phase/ordering note instead of a dependency).
* ``NAMED-REVIEW-MODEL <key> <field> <model>`` — a review pins a catalog model.
* ``MERGED-OPEN <key> <sha>`` — a commit on the project repo's main already
  carries ``Plan-Item: <key>`` while the item is still open.
* ``DUPLICATE-REPO <path> <names>`` — keeper.json registers one directory under
  several names (informational; merge-close reads it once).

Exit 0 ok; 1 with ``--strict`` and findings; 3 (``EXIT_UNREACHABLE``) when the
plan snapshot or keeper is unreachable (nothing is checked).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

from .cli_verbs import EXIT_OK, EXIT_UNREACHABLE, Unreachable
from .brief import missing_why_how
from .evidence_types import repo_identity
from .store_tiers import review_model_findings
from ..views import cancelled_gate_dependencies

#: A key or path-list mention is case-insensitive.
_TRAILER = "Plan-Item"
_GIT_TIMEOUT = 30.0
_CLOSED_STATES = ("done", "closed")

try:    # the planner's own overlap rule, when the package is importable
    from ..planner.readiness import _prefix_related
except ImportError:  # pragma: no cover - only when planner is absent
    def _prefix_related(a, b):
        n = min(len(a), len(b))
        return a[:n] == b[:n]


def _parts(path):
    return [c for c in str(path).split("/") if c and c != "."]


def _paths_of(item):
    """An item's declared ``paths``: the field, or the note line it folds into."""
    paths = item.get("paths")
    if isinstance(paths, str):
        return [paths]
    if isinstance(paths, list):
        return [str(p) for p in paths]
    note = item.get("note")
    if isinstance(note, str):
        for line in note.splitlines():
            name, _, value = line.partition(":")
            if name.strip() != "paths":
                continue
            try:
                decoded = json.loads(value.strip())
            except ValueError:
                return [x for x in value.split(",") if x.strip()]
            if isinstance(decoded, str):
                return [decoded]
            if isinstance(decoded, list):
                return [str(p) for p in decoded]
    return []


def _paths_touch(src, dst):
    src_paths, dst_paths = _paths_of(src), _paths_of(dst)
    return any(_prefix_related(_parts(a), _parts(b))
               for a in src_paths for b in dst_paths)


def _mentions(item, key):
    criteria = (item.get("done_criteria") or "").lower()
    return key.lower() in criteria


def order_only_edges(items):
    """``(src, dst)`` for every depends_on edge that only orders the plan."""
    by_key = {i["key"]: i for i in items if i.get("key")}
    findings = []
    for src in items:
        src_key = src.get("key")
        if not src_key:
            continue
        for dst_key in src.get("depends_on") or []:
            dst = by_key.get(dst_key)
            if dst is None or _paths_touch(src, dst) or _mentions(dst, src_key):
                continue
            if not (_paths_of(src) and _paths_of(dst)):
                continue    # undeclared paths: unknown, never claim order-only
            findings.append((src_key, dst_key))
    return sorted(findings)


def _repo_path(entry):
    if isinstance(entry, str):
        return entry, "main"
    if isinstance(entry, dict):
        return entry.get("path"), entry.get("ref") or "main"
    return None, "main"


def _repo_identity(entry):
    return repo_identity(*_repo_path(entry))


def duplicate_repos(repos):
    """``(path, [names])`` for every directory and ref registered under more than one name."""
    by_repo = {}
    for name, entry in sorted((repos or {}).items()):
        identity = _repo_identity(entry)
        if identity[0]:
            by_repo.setdefault(identity, []).append(name)
    return [(path, names) for (path, _ref), names in by_repo.items() if len(names) > 1]


def _valid_ref(ref):
    return isinstance(ref, str) and bool(ref) and not ref.startswith("-") \
        and ".." not in ref and re.fullmatch(r"[A-Za-z0-9._/-]+", ref)


def plan_item_trailers(repo, ref):
    """``[(sha, value)]`` for ``Plan-Item`` trailers reachable from *ref*."""
    if not repo or not _valid_ref(ref):
        return []
    cmd = ["git", "log", f"--format=%H%x00%(trailers:key={_TRAILER},valueonly)",
           ref]
    try:
        proc = subprocess.run(cmd, cwd=str(repo), capture_output=True,
                              timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    sha = ""
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        if "\x00" in line:      # commit line; later lines are its further trailers
            sha, _, line = line.partition("\x00")
            sha = sha.strip()
        value = line.strip()
        if value and sha:
            out.append((sha, value))
    return out


def merged_but_open(items, repos):
    """``(key, sha)`` for open items a merged commit already claims."""
    open_keys = {i["key"] for i in items
                 if i.get("key") and i.get("state") not in _CLOSED_STATES}
    findings = []
    for repo, ref in dict.fromkeys(_repo_identity(e) for e in (repos or {}).values()):
        for sha, value in plan_item_trailers(repo, ref):
            if value in open_keys:
                findings.append((value, sha))
    return sorted(findings)


def findings(items, repos):
    return order_only_edges(items), merged_but_open(items, repos)


def cmd_check(args, session):
    try:
        items = session.local_items()
    except Unreachable as exc:
        print(f"check needs a readable plan snapshot: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    config = getattr(session, "config", None)
    order_only, merged_open = findings(items, getattr(config, "repos", None))
    duplicates = duplicate_repos(getattr(config, "repos", None))
    cancelled_deps = cancelled_gate_dependencies(items)
    missing = missing_why_how(items)
    named_models = review_model_findings(items)
    if session.json_mode:
        print(json.dumps({
            "order_only": [{"src": src, "dst": dst} for src, dst in order_only],
            "merged_open": [{"key": key, "commit": sha} for key, sha in merged_open],
            "duplicate_repos": [{"path": path, "names": names} for path, names in duplicates],
            "missing_why_how": missing,
            "named_review_models": [{"key": key, "field": field, "model": model}
                                    for key, field, model in named_models],
            "cancelled_gate_deps": [{"gate": gate, "dependency": dep}
                                    for gate, dep in cancelled_deps],
        }))
    else:
        print(f"missing_why_how: {len(missing)} items" +
              (" " + ", ".join(missing[:20]) if missing else ""))
        for key, field, model in named_models:
            print(f"NAMED-REVIEW-MODEL {key} {field} {model}")
        for gate, dep in cancelled_deps:
            print(f"CANCELLED-GATE-DEP {gate} -> {dep}")
        for src, dst in order_only:
            print(f"ORDER-ONLY {src} -> {dst}")
        for key, sha in merged_open:
            print(f"MERGED-OPEN {key} {sha}")
        for path, names in duplicates:
            print(f"DUPLICATE-REPO {path} {', '.join(names)}")
    if getattr(args, "strict", False) and (order_only or merged_open or cancelled_deps or named_models):
        return 1
    return EXIT_OK


def add_parser(sub, make):
    p = make(sub, "check", help="plan hygiene: cancelled gate deps, order-only edges, merged-but-open items")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 when anything is found")


def add_go_row_parser(sub):
    p = sub.add_parser("go-row", help="check one GO row against the keeper's own grammar (offline)")
    p.add_argument("go_row_command", choices=["check"])
    p.add_argument("--row", help="the decision cell (else --file, else stdin)")
    p.add_argument("--file", help="read the decision cell from this file")
    p.add_argument("--repo", default=".", help="the repository whose train range is read")
    p.add_argument("--head", help="train head: the row must name exactly the Plan-Item keys of its commits")
    p.add_argument("--exclude", action="append", default=[],
                   help="commits already landed (repeatable; origin/main for a train)")
    return p


def cmd_go_row(args):
    """``pg go-row check``: prints ``accept`` or ``refuse: <code>: <reason>``; exit 0 or 2."""
    from .evidence_git import GitAdapter
    from .evidence_pr import train_trailer_keys
    from .store_runs import go_row_check

    row = args.row
    if row is None:
        row = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
    row = row.strip()
    delivered = deferred = None
    if args.head:
        try:
            delivered, deferred = train_trailer_keys(GitAdapter(), args.repo, args.head, args.exclude)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            print(f"refuse: go_row_train_unreadable: {exc}")
            return 2
    ok, reason = go_row_check(row, delivered=delivered, deferred=deferred or ())
    print("accept" if ok else f"refuse: {reason}")
    return EXIT_OK if ok else 2
