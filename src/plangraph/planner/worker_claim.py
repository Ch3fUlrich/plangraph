from __future__ import annotations

import os
from typing import Any

from plangraph.keeper.errors import Refused
from plangraph.planner import gitenv, phase_finish

__all__ = ["build_claim", "worker_claim"]

def build_claim(
    key: str, card: dict[str, Any], *, repo: str, base_sha: str
) -> dict[str, Any]:
    # RT-P1-CARD-CHECKS-KEEPER extends this builder next (check_kind,
    # check_reads, forbid from forbid_paths, RS-WIRING CardInput fields);
    # forbid stays [] until then.
    return {
        "card": key,
        "repo": repo,
        "base_sha": base_sha,
        "check_cmd": card["check_cmd"],
        "allow": list(card["write_set"]),
        "forbid": [],
        "prompt": card["outcome"],
    }

def worker_claim(
    store,
    principal,
    key: str,
    *,
    repo_root: str | None,
    base_branch: str = "main",
) -> dict[str, Any]:
    if principal.role not in ("worker", "system"):
        raise Refused("forbidden", "worker_claim requires a worker or system principal")
    if not repo_root or not os.path.isdir(repo_root):
        raise Refused("unavailable", "PG_PHASE_REPO_ROOT is not configured")

    row = store.conn.execute(
        "SELECT state, claimed_by, lease_until, project FROM items WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        raise Refused("not_found", f"item {key!r} not found")
    project = row[3]
    if principal.role == "worker" and project not in (principal.projects or []):
        raise Refused("not_found", f"item {key!r} not found")

    card = store.get_card(principal, key)
    if not card["check_cmd"]:
        raise Refused("bad_state", "card has no check_cmd")

    # Resolve the base before taking a lease: a git failure leaves no claim behind.
    gates = [
        r[0]
        for r in store.conn.execute(
            "SELECT e.src FROM edges e JOIN items i ON i.key = e.src "
            "WHERE e.dst = ? AND i.kind = 'gate' ORDER BY e.src",
            (key,),
        ).fetchall()
    ]
    base_sha = None
    for gate in gates:
        tip = gitenv.rev_parse(
            repo_root, "refs/heads/" + phase_finish.phase_branch(gate)
        )
        if tip is not None:
            base_sha = tip
            break
    if base_sha is None:
        base_sha = gitenv.rev_parse(repo_root, "refs/heads/" + base_branch)
    if base_sha is None:
        raise Refused("unavailable", "no base to claim from")

    state, claimed_by, lease_until, _project = row
    lease_live = False
    if state == "claimed" and claimed_by == principal.name and lease_until:
        try:
            lease_dt = store._parse_iso(lease_until)
        except (TypeError, ValueError):
            lease_dt = None
        if lease_dt is not None and lease_dt.timestamp() > store.now():
            lease_live = True
    if not lease_live:
        store.claim(principal, key)

    return build_claim(key, card, repo=repo_root, base_sha=base_sha)
