"""Keeper half of the Phase runner (S3): land a finished phase branch.

The worker has completed the cards on ``phase/<key>``; this verb pushes the
branch, opens or reuses its pull request, waits for CI on exactly the supplied
head, then merges with the keeper's own credential.  It is idempotent per phase
and records merge evidence on the phase's cards after a confirmed merge.
"""
from __future__ import annotations

import fcntl
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, TextIO

from plangraph.keeper.errors import Refused
from plangraph.keeper import events
from plangraph.keeper.evidence_git import GitAdapter
from plangraph.planner import gitenv

__all__ = ["GitHub", "GitHubError", "finish_phase", "phase_branch", "attach_merge_evidence"]

_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
# Git ref components may be sanitised to this alphabet by the worker.
_BRANCH_BAD_RE = re.compile(r"[^A-Za-z0-9._-]")

class GitHubError(Exception):
    """Adapter failure: network, 4xx/5xx, or a malformed reply."""

class GitHub(Protocol):
    """Minimal GitHub adapter; the real implementation is a later slice."""

    def push_branch(self, repo_root: Path, branch: str, head: str) -> None: ...
    def find_pr(self, branch: str) -> dict[str, Any] | None: ...
    def open_pr(self, branch: str, base: str, title: str, body: str) -> dict[str, Any]: ...
    def ci_green(self, head: str) -> bool: ...
    def merge_pr(self, number: int, head: str) -> dict[str, Any]: ...

def phase_branch(key: str) -> str:
    """Return the worker-compatible branch name for phase ``key``."""
    clean = _BRANCH_BAD_RE.sub("-", key)
    if not clean:
        raise Refused("validation", "phase key is empty")
    if len(clean) > 120:
        raise Refused("validation", "phase key is longer than 120 characters")
    if clean.startswith(".") or clean.endswith(".") or clean.endswith(".lock"):
        raise Refused("validation", "invalid phase branch name")
    if ".." in clean:
        raise Refused("validation", "invalid phase branch name")
    return f"phase/{clean}"

def _check_local_tip(root: Path, branch: str, head: str) -> None:
    tip = gitenv.rev_parse(root, f"refs/heads/{branch}")
    if tip is None:
        raise Refused("unavailable", "local phase branch is missing")
    if tip != head:
        raise Refused("conflict", "local branch tip differs from head")

def _acquire_lock(root: Path, lock_path: Path | str | None) -> TextIO:
    path = Path(lock_path) if lock_path is not None else (
        root / ".git" / "plangraph-phase-finish.lock"
    )
    fh: TextIO | None = None
    try:
        fh = open(path, "a")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fh is not None:
            fh.close()
        raise Refused("conflict", "phase_finish already running for this repo")
    except OSError as exc:
        if fh is not None:
            fh.close()
        raise Refused("unavailable", f"phase_finish lock: {exc}")
    return fh

def _result(key: str, branch: str, head: str, state: str, pr: dict[str, Any],
            cards: list[str]) -> dict[str, Any]:
    result = {
        "phase": key,
        "branch": branch,
        "head": head,
        "state": state,
        "pr": {"number": pr["number"], "url": pr["url"]},
        "cards": cards,
    }
    if pr.get("merge_sha") is not None:
        result["merge_sha"] = pr["merge_sha"]
    return result

def attach_merge_evidence(store, principal, gate: str, merge_sha: str, *,
                          github: GitHub, repo_root: Path | str | None = None) -> None:
    """Atomically attach verified proof of this phase's exact merge, once per card."""
    if principal.role not in ("router", "system"):
        raise Refused("forbidden", "merge evidence requires router or system")
    if not isinstance(merge_sha, str) or _SHA_RE.fullmatch(merge_sha) is None:
        raise Refused("validation", "merge_sha must be a 40-character lowercase hex sha")
    row = store.conn.execute(
        "SELECT kind, state FROM items WHERE key=?", (gate,)).fetchone()
    if row is None:
        raise Refused("not_found", f"item not found: {gate}")
    if row[0] != "gate":
        raise Refused("validation", f"not a gate: {gate}")
    if row[1] == "cancelled":
        raise Refused("bad_state", "gate is cancelled")
    try:
        pr = github.find_pr(phase_branch(gate))
    except GitHubError as exc:
        raise Refused("unavailable", str(exc))
    if pr is None or pr["state"] != "merged":
        raise Refused("bad_state", "phase PR is not merged")
    head = pr.get("head")
    if (pr.get("merge_sha") != merge_sha or not isinstance(head, str)
            or _SHA_RE.fullmatch(head) is None):
        raise Refused("conflict", "merge proof differs from phase PR or is missing")
    if repo_root is not None:
        adapter = GitAdapter()
        try:
            parents = adapter._run(str(repo_root), [
                "rev-list", "--parents", "-n", "1", "--end-of-options", merge_sha])
            tokens = parents.stdout.strip().split() if parents.returncode == 0 else []
        except (OSError, subprocess.SubprocessError):
            tokens = []
        if len(tokens) < 3 or tokens[0] != merge_sha or tokens[2] != head:
            raise Refused("conflict", "merge second parent differs from phase PR head")
    store._run(principal, "attach_merge_evidence", gate,
               lambda now: _attach_cards(store, principal, gate, merge_sha, now))

def _attach_cards(store, principal, gate, merge_sha, now):
    ref = {"commit": merge_sha}
    encoded = events.canonical(ref)

    def exists(key):
        return store.conn.execute(
            "SELECT 1 FROM evidence WHERE key=? AND kind='commit+ci' AND ref=?",
            (key, encoded)).fetchone() is not None

    def cards():
        return [row[0] for row in store.conn.execute(
            "SELECT dst FROM edges WHERE src=? ORDER BY dst", (gate,)).fetchall()]

    keys = cards()
    prepared = {}
    # All slow verification precedes the lock; no card can leave partial proof.
    for key in keys:
        if exists(key):
            continue
        item = store._get_or_refuse(key)
        store._assert_unlocked(item)
        if item["state"] not in ("claimed", "in_review"):
            raise Refused("bad_state", f"{key} is {item['state']}")
        result = store._verify_evidence(item, "commit+ci", ref, now)
        if not result.ok:
            raise Refused("bad_state", f"{key}: {result.detail}")
        prepared[key] = (item, result)
    with store._write():
        gate_item = store._get_or_refuse(gate)
        if gate_item["state"] == "cancelled" or cards() != keys:
            raise Refused("conflict", "phase changed during evidence verification")
        for key in keys:
            if exists(key):
                continue
            old, result = prepared[key]
            fresh = store._get_or_refuse(key)
            store._assert_unlocked(fresh)
            if fresh["state"] not in ("claimed", "in_review") or fresh["epoch"] != old["epoch"]:
                raise Refused("conflict", f"{key} changed during evidence verification")
            store._guarded_touch(key, ("claimed", "in_review"), fresh["epoch"])
            store._record_evidence(principal, fresh, "commit+ci", ref, now, result)

def _finish_locked(key: str, branch: str, head: str, root: Path,
                   github: GitHub, base: str, cards: list[str]) -> dict[str, Any]:
    try:
        pr = github.find_pr(branch)
        if pr is not None and pr["state"] == "merged":
            if pr["head"] != head:
                # a merged PR for another head is a conflict, not our merge
                raise Refused("conflict", "merged PR head differs from requested head")
            return _result(key, branch, head, "merged", pr, cards)

        github.push_branch(root, branch, head)
        if pr is None or pr["state"] == "closed":
            body = (
                f"Gate: {key}\n"
                f"Head: {head}\n"
                f"Cards: {', '.join(cards)}"
            )
            pr = github.open_pr(branch, base, title=f"{key}: phase branch", body=body)

        if not github.ci_green(head):
            return _result(key, branch, head, "waiting_ci", pr, cards)

        merged = github.merge_pr(pr["number"], head)
        return _result(key, branch, head, "merged", merged, cards)
    except GitHubError as exc:
        raise Refused("unavailable", str(exc))

def finish_phase(store, principal, key: str, branch: str, head: str, *,
                 repo_root: Path | str, github: GitHub, base: str = "main",
                 lock_path: Path | str | None = None) -> dict[str, Any]:
    """Land the gate's phase branch after CI is green on exactly ``head``."""
    # Role gate comes first: do not read the store or filesystem before this.
    if principal.role not in ("router", "system"):
        raise Refused("forbidden", "phase_finish requires router or system")

    expected_branch = phase_branch(key)
    if branch != expected_branch:
        raise Refused("validation", f"branch must be {expected_branch}")
    if _SHA_RE.fullmatch(head) is None:
        raise Refused("validation", "head must be a 40-character lowercase hex sha")

    row = store.conn.execute(
        "SELECT kind, state FROM items WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        raise Refused("not_found", f"item not found: {key}")
    kind, gate_state = row[0], row[1]
    if kind != "gate":
        raise Refused("validation", f"not a gate: {key}")
    if gate_state == "cancelled":
        raise Refused("bad_state", "gate is cancelled")

    edge_rows = store.conn.execute(
        "SELECT dst FROM edges WHERE src = ?", (key,)
    ).fetchall()
    cards = sorted({edge[0] for edge in edge_rows})
    if not cards:
        raise Refused("bad_state", "gate has no cards")

    not_done: list[str] = []
    for card in cards:
        card_row = store.conn.execute(
            "SELECT state FROM items WHERE key = ?", (card,)
        ).fetchone()
        if card_row is None or card_row[0] != "in_review":
            not_done.append(card)
    if not_done:
        raise Refused("bad_state", f"cards not worker-complete: {', '.join(not_done)}")

    root = Path(repo_root)
    _check_local_tip(root, branch, head)

    # Serialise keeper-side merges per repository; the lock is advisory only.
    lock_file = _acquire_lock(root, lock_path)
    try:
        result = _finish_locked(key, branch, head, root, github, base, cards)
        if result["state"] == "merged" and result.get("merge_sha") is not None:
            # The merge reply already confirms this PR; avoid a stale follow-up read.
            confirmed = SimpleNamespace(find_pr=lambda branch: {
                "state": "merged", "head": result["head"],
                "merge_sha": result["merge_sha"]})
            attach_merge_evidence(store, principal, key, result["merge_sha"],
                                  github=confirmed, repo_root=root)
        return result
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()
