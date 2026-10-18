"""``pg check`` (PLAN-HYGIENE): read-only checks over the local plan snapshot.

Two findings, printed as one line each:

* ``ORDER-ONLY <src> -> <dst>`` — ``src`` waits on ``dst``, but nothing in
  ``dst``'s done criteria needs ``src``'s code and their declared ``paths``
  touch different trees, so the edge only orders the plan (it could be a
  phase/ordering note instead of a dependency).
* ``MERGED-OPEN <key> <sha>`` — a commit on the project repo's main already
  carries ``Plan-Item: <key>`` while the item is still open.

Exit 0 unless ``--strict`` is passed and something was found.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

from .cli_verbs import EXIT_OK, Unreachable

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
            findings.append((src_key, dst_key))
    return sorted(findings)


def _repo_path(entry):
    if isinstance(entry, str):
        return entry, "main"
    if isinstance(entry, dict):
        return entry.get("path"), entry.get("ref") or "main"
    return None, "main"


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
                              text=True, timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        sha, _, value = line.partition("\x00")
        value = value.strip()
        if value:
            out.append((sha.strip(), value))
    return out


def merged_but_open(items, repos):
    """``(key, sha)`` for open items a merged commit already claims."""
    open_keys = {i["key"] for i in items
                 if i.get("key") and i.get("state") not in _CLOSED_STATES}
    findings = []
    for entry in (repos or {}).values():
        repo, ref = _repo_path(entry)
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
        return 3
    config = getattr(session, "config", None)
    order_only, merged_open = findings(items, getattr(config, "repos", None))
    if session.json_mode:
        print(json.dumps({
            "order_only": [{"src": src, "dst": dst} for src, dst in order_only],
            "merged_open": [{"key": key, "commit": sha} for key, sha in merged_open],
        }))
    else:
        for src, dst in order_only:
            print(f"ORDER-ONLY {src} -> {dst}")
        for key, sha in merged_open:
            print(f"MERGED-OPEN {key} {sha}")
    if getattr(args, "strict", False) and (order_only or merged_open):
        return 1
    return EXIT_OK


def add_parser(sub, make):
    p = make(sub, "check", help="plan hygiene: order-only edges, merged-but-open items")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 when anything is found")
