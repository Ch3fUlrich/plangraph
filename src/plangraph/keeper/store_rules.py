"""PG-EFFICIENCY-GATES slice 2 (operator D-2746): decisions live in the keeper.

``rule_set(rules)`` writes standing rules as ``decision`` items of project
``decisions`` in one transaction.  A rule in force is ``done``, a retired one
``cancelled`` (locked: retiring is final), so rules never show in ready / blocked / stall / operator-wait
views and no claim or review applies.  Each write appends one ``rule_set``
event per key (state + description; ``events.fold`` replays it), creating the
item first (``item_created``) when the key is new.  ``depends_on`` edges between
rules form the decision tree (a DAG; cycles and unknown targets are refused).
``plangraph.decisions_view`` renders ``DECISIONS.md`` from these items.  Router
only.
"""
from __future__ import annotations

import re

from .credential_guard import guard
from .errors import Refused
from .note_fold import DESCRIPTION_CAP

__all__ = ["RuleMixin", "RULE_PROJECT", "RULE_ID", "RULE_STATES"]

RULE_PROJECT = "decisions"
#: ``PREAMBLE``, ``SECTION-<n>`` or ``RULE-<n>-<m>[<a-z>]`` (upper-cased).
RULE_ID = re.compile(r"(?:PREAMBLE|SECTION-\d+|RULE-\d+-\d+[A-Z]?)")
RULE_STATES = {"in_force": "done", "retired": "cancelled"}
MAX_RULES = 400
_CAPS = {"title": 200, "source": 200, "note": 500}


def _rule(raw):
    if not isinstance(raw, dict):
        raise Refused("validation", "each rule must be an object")
    rid = raw.get("id")
    if not isinstance(rid, str) or not RULE_ID.fullmatch(rid):
        raise Refused("validation", f"rule id {rid!r} must be PREAMBLE, SECTION-<n> or RULE-<n>-<m>[A-Z]")
    key = f"{RULE_PROJECT}:{rid}"
    unknown = set(raw) - {"id", "title", "description", "source", "note", "depends_on", "state"}
    if unknown:
        raise Refused("validation", f"{key}: unknown rule field(s) {sorted(unknown)}")
    text = raw.get("description")
    if not isinstance(text, str) or not text.strip() or len(text) > DESCRIPTION_CAP:
        raise Refused("validation", f"{key}: description must be 1..{DESCRIPTION_CAP} characters")
    guard("description", text, key)
    out = {"id": rid, "key": key, "description": text}
    for name, cap in _CAPS.items():
        value = raw.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > cap):
            raise Refused("validation", f"{key}: {name} must be a string of <= {cap} characters")
        if value:
            guard(name, value, key)
        out[name] = value
    state = raw.get("state", "in_force")
    if state not in RULE_STATES:
        raise Refused("validation", f"{key}: state must be one of {sorted(RULE_STATES)}")
    out["state"] = RULE_STATES[state]
    deps = raw.get("depends_on") or []
    if not isinstance(deps, list) or not all(isinstance(d, str) and RULE_ID.fullmatch(d) for d in deps):
        raise Refused("validation", f"{key}: depends_on must list rule ids")
    out["depends_on"] = sorted({f"{RULE_PROJECT}:{d}" for d in deps} - {key})
    return out


def _check_dag(edges):
    """Refuse a cycle in ``{key: [dep, ...]}`` (design invariant I9)."""
    done, active = set(), set()

    def visit(node, path):
        if node in done:
            return
        if node in active:
            raise Refused("validation", f"rule tree cycle: {' -> '.join(path + [node])}")
        active.add(node)
        for dep in edges.get(node, ()):
            visit(dep, path + [node])
        active.discard(node)
        done.add(node)

    for node in sorted(edges):
        visit(node, [])


class RuleMixin:
    """``rule_set`` for :class:`plangraph.keeper.store.Store`."""

    def rule_set(self, principal, rules):
        return self._run(principal, "rule_set", None,
                         lambda now: self._rule_set(principal, rules, now))

    def _rule_edges(self):
        edges = {}
        for row in self.conn.execute(
                "SELECT e.src, e.dst FROM edges e JOIN items i ON i.key = e.src WHERE i.project = ?",
                (RULE_PROJECT,)):
            edges.setdefault(row["src"], []).append(row["dst"])
        return edges

    def _rule_set(self, principal, rules, now):
        self._require_role(principal, "router")
        if not isinstance(rules, list) or not 0 < len(rules) <= MAX_RULES:
            raise Refused("validation", f"rules must be a list of 1..{MAX_RULES} rule objects")
        batch = [_rule(raw) for raw in rules]
        keys = [r["key"] for r in batch]
        if len(set(keys)) != len(keys):
            raise Refused("validation", "a rule id appears twice in the batch")
        with self._write():
            existing = {}
            for r in batch:
                row = self.conn.execute("SELECT key, kind, state FROM items WHERE key=?",
                                        (r["key"],)).fetchone()
                if row is not None and row["kind"] != "decision":
                    raise Refused("conflict", f"{r['key']} exists and is kind {row['kind']!r}")
                if row is not None and row["state"] == "cancelled":
                    # cancelled keys are locked (schema triggers): retiring is final.
                    raise Refused("locked", f"{r['key']} is retired; add the rule under a new id")
                existing[r["key"]] = row is not None
            known = {row["key"] for row in self.conn.execute(
                "SELECT key FROM items WHERE project = ?", (RULE_PROJECT,))} | set(keys)
            edges = self._rule_edges()
            for r in batch:
                missing = [d for d in r["depends_on"] if d not in known]
                if missing:
                    raise Refused("not_found", f"{r['key']}: depends_on unknown rule(s) {missing}")
                edges[r["key"]] = r["depends_on"]
            _check_dag(edges)
            created, updated = [], []
            for r in batch:
                if not existing[r["key"]]:
                    self._insert_item(principal, {
                        "key": r["key"], "project": RULE_PROJECT, "id": r["id"],
                        "title": r["title"] or r["id"], "description": r["description"],
                        "kind": "decision", "operator": False, "wait": None,
                        "evidence_kinds": ["decision"], "review_tier": "standard",
                        "source": r["source"], "note": r["note"], "state": "open"}, now)
                    created.append(r["key"])
                else:
                    updated.append(r["key"])
                # edges + event before the row: a retire makes the key locked.
                self.conn.execute("DELETE FROM edges WHERE src=?", (r["key"],))
                for dep in r["depends_on"]:
                    self.conn.execute("INSERT INTO edges(src,dst,rel) VALUES(?,?, 'depends_on')",
                                      (r["key"], dep))
                self._append(principal, r["key"], "rule_set",
                             {"state": r["state"], "description": r["description"],
                              "depends_on": r["depends_on"]}, now)
                # No exec clear here (qwen-R3): a rule row has no exec
                # lifecycle to hand back.  Rules have "no lifecycle
                # (claim/review) applies" (module docstring above; design table
                # PG-EFFICIENCY-GATES, "in force / retired" row) -- a rule row
                # is done/cancelled from its creating transaction, so it is
                # never claimable and ``_exec_check`` can never record an exec
                # owner on it (it records only for the holder of a claimed
                # card).  With no owner ever to clear, a clear would only fence
                # a run this item type has no notion of; the guard covers the
                # write through the explicit STATE_WRITER_EXCEPTIONS entry
                # (tests/test_keeper_exec_clear_guard.py) instead.  Design I8
                # needs nothing more: latest ``rule_set`` wins on replay.
                self.conn.execute(
                    "UPDATE items SET state=?, title=?, description=?, source=?, note=?,"
                    " lease_until=NULL, updated_at=? WHERE key=?",
                    (r["state"], r["title"] or r["id"], r["description"], r["source"],
                     r["note"], self._iso(now), r["key"]))
        return {"created": created, "updated": updated}
