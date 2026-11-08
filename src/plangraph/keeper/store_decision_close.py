"""Origin-main decision proof for keeper closes; no router batch required."""
import hashlib
import json
import re
import subprocess

from .errors import Refused
from .timing import outside_request_lock

LOG_PATH = "router/docs/decisions-log.md"


def _authorize(store, principal, item, epoch):
    if principal.role == "router":
        return
    if item.get("kind") == "operator_task":
        raise Refused("forbidden", "operator_task requires router or operator answer")
    if item.get("claimed_by") != principal.name:
        raise Refused("forbidden", "only the current claimant or router may close")
    if epoch is None:
        raise Refused("epoch_stale", "claim epoch is required")
    store._epoch_ok(item, epoch)


def _row(store, item, now, decision):
    git = store._ctx(item, now).git
    try:
        repo = git.repo_path("decisions")
    except Exception:  # noqa: BLE001 - adapter failures must refuse
        repo = None
    if not repo:
        raise Refused("evidence_unverified", "decisions repository is not configured")
    with outside_request_lock():
        return _fetch_row(repo, decision)


def _fetch_row(repo, decision):
    try:
        subprocess.run(["git", "-C", str(repo), "fetch", "origin", "main"],
                       check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise Refused("evidence_unverified", "decisions origin/main fetch failed") from None
    try:
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "origin/main^{commit}"],
            check=True, capture_output=True, timeout=30).stdout.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("invalid origin/main sha")
        text = subprocess.run(
            ["git", "-C", str(repo), "show", f"{sha}:{LOG_PATH}"],
            check=True, capture_output=True, timeout=30).stdout.decode("utf-8")
    except (OSError, ValueError, subprocess.SubprocessError):
        raise Refused("evidence_unverified", "decisions origin/main log is unreadable") from None
    rows = [line for line in text.splitlines() if line.startswith(f"| {decision} |")]
    if len(rows) != 1:
        raise Refused("evidence_unverified", "decision row missing or duplicated on origin/main")
    return sha, rows[0]


def _evidence(store, key, item, decision):
    rows = [e for e in store._evidence(key)
            if e["epoch"] == item["epoch"] and e["kind"] == "decision"
            and (e.get("ref") or {}).get("id") == decision]
    if not rows:
        raise Refused("evidence_missing", "item has no matching decision evidence")


def _proof(store, key, item, decision, row):
    _evidence(store, key, item, decision)
    tokens = [token.rstrip(".") for token in re.findall(
        r"(?<![\w])CLOSE ([A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+)(?![\w:.-])", row)]
    if key not in tokens:
        raise Refused("evidence_unverified", f"decision row has no exact CLOSE {key} token")
    return hashlib.sha256(row.encode("utf-8")).hexdigest()


def _item_digest(item):
    return hashlib.sha256(json.dumps(dict(item), sort_keys=True).encode("utf-8")).hexdigest()


def close_decision(store, principal, key, decision, epoch, batch_ref, now):
    if batch_ref is not None:
        raise Refused("validation", "choose decision_ref or batch_ref, not both")
    if not isinstance(decision, str) or not re.fullmatch(r"D-[0-9]{4,}", decision):
        raise Refused("validation", "decision_ref must be D-NNNN (no local or non-main ref)")
    item = store._get_or_refuse(key)
    item_digest = _item_digest(item)
    _authorize(store, principal, item, epoch)
    _evidence(store, key, item, decision)
    sha, row = _row(store, item, now, decision)
    digest = _proof(store, key, item, decision, row)
    with store._write():
        fresh = store._get_or_refuse(key)
        if fresh["epoch"] != item["epoch"]:
            raise Refused("conflict", "claim epoch changed during close")
        if _item_digest(fresh) != item_digest:
            raise Refused("conflict", "item row changed during close")
        store._assert_unlocked(fresh)
        _authorize(store, principal, fresh, epoch)
        if fresh["state"] not in ("claimed", "in_review"):
            raise Refused("bad_state", "decision close requires an active item")
        if fresh.get("review_tier") != "none" and fresh.get("kind") not in (
                "decision", "operator_task"):
            raise Refused("bad_state", "item kind/tier cannot close on a decision")
        if "commit" in (fresh.get("evidence_kinds") or []):
            store._check_commit_evidence(key, fresh, now)
        cursor = store.conn.execute(
            "UPDATE items SET state='done', reviewer=?, updated_at=? WHERE key=? AND epoch=?",
            (principal.name, store._iso(now), key, fresh["epoch"]))
        store._race_guard(cursor, key, fresh["state"], fresh["epoch"])
        payload = {"decision_ref": decision, "decision_ids": [decision],
                   "row_sha256": digest, "decision_commit_sha": sha}
        store._append(principal, key, "closed_on_decision", payload, now)
        return {"key": key, "state": "done", **payload}
