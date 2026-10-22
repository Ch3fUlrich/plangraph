"""Keeper half of the Phase runner (S3): land a finished phase branch.

The worker has completed the cards on ``phase/<key>``; this verb pushes the
branch, opens or reuses its pull request, waits for CI on exactly the supplied
head, then merges with the keeper's own credential.  It is idempotent per phase
and performs no store write.
"""
from __future__ import annotations

import fcntl
import re
import subprocess
from pathlib import Path
from typing import Any, Protocol, TextIO

from plangraph.keeper.errors import Refused

__all__ = ["GitHub", "GitHubError", "finish_phase", "phase_branch"]

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
    cmd = ["git", "-C", str(root), "rev-parse", "--verify", f"refs/heads/{branch}"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused("unavailable", f"git rev-parse failed: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise Refused("unavailable", f"git rev-parse failed: {detail}")
    if proc.stdout.strip() != head:
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
    return {
        "phase": key,
        "branch": branch,
        "head": head,
        "state": state,
        "pr": {"number": pr["number"], "url": pr["url"]},
        "cards": cards,
    }

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
        if card_row is None or card_row[0] != "done":
            not_done.append(card)
    if not_done:
        raise Refused("bad_state", f"cards not done: {', '.join(not_done)}")

    root = Path(repo_root)
    _check_local_tip(root, branch, head)

    # Serialise keeper-side merges per repository; the lock is advisory only.
    lock_file = _acquire_lock(root, lock_path)
    try:
        return _finish_locked(key, branch, head, root, github, base, cards)
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()
