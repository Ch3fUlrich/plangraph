"""md_sync plan: parsed markdown + live snapshot -> an ordered keeper ops list.

Every text field that leaves here is masked with the keeper's own credential
guard first (D-405): the store refuses a ``word-slash-secret`` pair near a
credential keyword (D-374), so md_sync masks it, counts the mask as a redaction
and never sends or prints the original.
"""
import os
import re
import sys
from collections import Counter

import md_sync_parse as mp

# Import the guard from the checkout so tools/ works without an install step.
_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    os.pardir, "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from plangraph.keeper import credential_guard  # noqa: E402  (D-405)
from plangraph.keeper.evidence_decisions import DecisionsAdapter  # noqa: E402

import md_sync_dclose  # noqa: E402  (D-505: router D-rows with Closes key@sha)

PROJECT = "routing"
GATE_ID = "PH-OPERATOR"
GATE_KEY = f"{PROJECT}:{GATE_ID}"
GOAL_KEY = f"{PROJECT}:GOAL"
SOURCE = "md-sync"
#: the cap the keeper puts on the ``description`` column
#: (``plangraph.keeper.note_fold.DESCRIPTION_CAP``).
DESCRIPTION_CAP = 2000
#: MODEL-NAMES (4fb3167) made ``description`` a column; before that the
#: keeper folded it into ``note`` as a first line with this prefix.
FOLDED_PREFIX = "description: "
_FIELD_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*: ")
ELLIPSIS = "…"


class PlanError(Exception):
    """The live graph cannot take the import (no goal, gate closed, ...)."""


class Plan:
    def __init__(self):
        self.ops = []
        self.counts = Counter()
        self.skipped = []   # (id, reason): nothing done, worth reporting
        self.refused = []   # (id, reason): cited something that does not exist
        self.gone = []      # keys of our open items no longer in QUESTIONS
        #: (id, decision_id, key): a close the keeper would refuse (Closes:
        #: keyword without the key), so the whole item was left untouched
        self.close_skipped = []
        #: D-505: the router D-row pass (tools/md_sync_dclose.py)
        self.d_closes = []       # {key, decision}: a close the D-rows planned
        self.d_in_review = []    # {key, decision, sha, reason}: accepted-but-unmerged
        self.d_lines = []        # one report line per planned D-row item
        self.dclose_skipped = [] # (id, D-n, key, reason): D-row close "not in graph"
        self.warnings = []       # {key, decision, text}: D-505/D-547 warnings
        self.models = []         # {key, decision, text}: Models trailers copied
        self.invalid_tokens = [] # D-ids whose Closes tokens the keeper parser marked invalid
        self.redactions = 0

    def add(self, op):
        self.ops.append(op)
        self.counts[op["op"]] += 1


def live_description(item):
    """The live item's description: its column, else its pre-MODEL-NAMES note line.

    Before MODEL-NAMES the keeper folded ``description`` into ``note`` as a
    first ``description: `` line, so an item created back then still carries it
    there until ``migrate-descriptions`` moves it.  Read it exactly like
    ``plangraph.keeper.store_names.split_folded_description`` does: the first
    line plus its continuation lines, up to the next ``name: value`` line.
    """
    description = item.get("description")
    if description:
        return description
    note = item.get("note")
    if not isinstance(note, str) or not note.startswith(FOLDED_PREFIX):
        return ""
    lines = note.split("\n")
    taken = [lines[0][len(FOLDED_PREFIX):]]
    rest = 1
    while rest < len(lines) and not _FIELD_LINE.match(lines[rest]):
        taken.append(lines[rest])
        rest += 1
    return "\n".join(taken)


def _fit(description):
    """Cap at the keeper's ``description`` column cap (never over it)."""
    if len(description) <= DESCRIPTION_CAP:
        return description
    return description[:DESCRIPTION_CAP - len(ELLIPSIS)].rstrip() + ELLIPSIS


def _index(items):
    by_id = {}
    for item in items:
        by_id.setdefault(item["id"], []).append(item)
    return by_id


def _find(by_id, ident):
    """The live item for *ident*: ``routing:`` first, then any project."""
    found = sorted(by_id.get(ident, []), key=lambda i: i["key"])
    mine = [i for i in found if i["project"] == PROJECT]
    pool = mine or [i for i in found if i["state"] != "cancelled"]
    return (pool or [None])[0]


def _decision_ids(item):
    out = set()
    for row in item.get("evidence") or []:
        ref = row.get("ref")
        if row.get("kind") == "decision":
            out.add(ref.get("id") if isinstance(ref, dict) else ref)
    return out


def build_plan(questions, answers, skipped_answers, decision_ids, items,
               decisions_view=None, decisions_text=None, repos=None, since=1):
    """``decisions_view`` is the keeper's own ``DecisionsAdapter`` over the
    decisions file (or ``None``: no ``Closes:`` pre-check, as before).

    With ``decisions_text`` (the raw decisions log) the router D-row pass runs
    too (D-505): its ``Closes: key@sha`` rows close only on a merged commit,
    with ``repos`` (the keeper config's ``repos``) and ``since`` (ignore D-rows
    below it) selecting the merge gate.
    """
    plan = Plan()
    by_id = _index(items)
    state = {i["key"]: i for i in items}
    plan.skipped.extend((s.id, s.reason) for s in skipped_answers)
    pending = {}  # id -> key, created in this run
    new_lanes, seen = [], set()
    for entry in questions:
        if entry.id in seen:
            plan.skipped.append((entry.id, "duplicate in QUESTIONS"))
            continue
        seen.add(entry.id)
        title, n1 = mp.redact(entry.title)
        desc, n2 = mp.redact(entry.description)
        title, m1 = credential_guard.mask(title)
        desc, m2 = credential_guard.mask(desc)
        plan.redactions += n1 + n2 + m1 + m2
        desc = _fit(desc)
        item = _find(by_id, entry.id)
        if item is None:
            lane = {"project": PROJECT, "id": entry.id, "title": title,
                    "kind": "lane", "track": "QUESTIONS", "owner": "operator",
                    "operator": True, "evidence_kinds": ["decision"],
                    "review_tier": "none", "depends_on": [], "source": SOURCE}
            if desc:
                lane["description"] = desc
            plan.add({"op": "create", "item": lane})
            key = f"{PROJECT}:{entry.id}"
            pending[entry.id] = key
            new_lanes.append(key)
        elif item["project"] == PROJECT and item.get("source") == SOURCE \
                and item["state"] == "open":
            fields = {}
            if item.get("title") != title:
                fields["title"] = title
            current = live_description(item)
            if current != desc and (desc or item.get("description")):
                # An empty markdown description only clears a real column: a
                # folded note line belongs to ``migrate-descriptions``, and
                # clearing ``note`` here would be undone by the fallback above
                # on the very next run (no idempotence).
                fields["description"] = desc
            if fields:
                plan.add({"op": "edit", "key": item["key"], "fields": fields})
    _link(plan, state, new_lanes)
    dinfo, invalid_rows = ({}, [])
    if decisions_text is not None and decisions_view is not None:
        dinfo, invalid_rows = md_sync_dclose.collect(decisions_text,
                                                     decisions_view, since)
    _answers(plan, answers, decision_ids, by_id, pending, decisions_view,
             dinfo=dinfo or None, owned=set(dinfo))
    if decisions_text is not None and decisions_view is not None:
        md_sync_dclose.finalize(dinfo)
        by_key = {item["key"]: item for item in items}
        md_sync_dclose.plan(plan, dinfo, invalid_rows, by_key=by_key,
                            pending_keys=set(pending.values()), repos=repos)
    answered = {a.id for a in answers}
    for item in items:
        if (item["project"] == PROJECT and item.get("source") == SOURCE
                and item["state"] == "open" and item["id"] not in seen
                and item["id"] not in answered):
            plan.gone.append(item["key"])
    _mask_guarded(plan)
    return plan


def _mask_guarded(plan):
    """D-405: mask a credential shape in every guarded field any op carries.

    Title and description are masked as they are read (so edits compare masked
    with masked and stay idempotent); this sweep is the structural guarantee
    that no op leaves md_sync carrying a shape the keeper would refuse --
    including fields the plan never redacts, like ``done_criteria`` or the
    folded ``note`` of a ``legacy_state``.  Counts join ``plan.redactions``.
    """
    for op in plan.ops:
        for holder in (op, op.get("item"), op.get("fields")):
            if not isinstance(holder, dict):
                continue
            for field in credential_guard.GUARDED_FIELDS:
                value = holder.get(field)
                if isinstance(value, str):
                    holder[field], n = credential_guard.mask(value)
                    plan.redactions += n
    for entry in plan.models:
        entry["text"], _ = credential_guard.mask(entry["text"])


def _link(plan, state, new_lanes):
    """Reachability: new lanes hang off the gate; a new gate hangs off the goal."""
    if not new_lanes:
        return
    gate = state.get(GATE_KEY)
    if gate is None:
        goal = state.get(GOAL_KEY)
        if goal is None or goal["state"] != "open":
            raise PlanError(f"{GOAL_KEY} must exist and be open to link {GATE_KEY}")
        plan.add({"op": "create", "item": {
            "project": PROJECT, "id": GATE_ID, "title": "Operator questions and steps",
            "kind": "gate", "track": "QUESTIONS", "owner": "operator",
            "depends_on": list(new_lanes), "source": SOURCE}})
        plan.add({"op": "edit", "key": GOAL_KEY, "fields": {
            "depends_on": list(goal.get("depends_on") or []) + [GATE_KEY]}})
    else:
        if gate["state"] != "open":
            raise PlanError(f"{GATE_KEY} is {gate['state']}: new lanes would be unreachable")
        plan.add({"op": "edit", "key": GATE_KEY, "fields": {
            "depends_on": list(gate.get("depends_on") or []) + list(new_lanes)}})


def _closes_mismatch(view, decisions, key):
    """Decisions whose keeper-owned ``Closes:`` list omits *key* (may refuse).

    The token parsing is the keeper's own (``DecisionsAdapter.closes`` /
    ``has_closes_keyword``), so md_sync and the keeper cannot diverge: a row
    with a ``Closes:`` keyword whose whole tokens do not include *key* makes
    ``close_on_decision`` refuse, and a malformed keyword (L5) refuses too.
    """
    if view is None:
        return []
    return [dec for dec in decisions
            if view.has_closes_keyword(dec) and key not in view.closes(dec)]


def _contribute_drows(plan, key, ident, decisions, view, dinfo):
    """Join an ANSWERS entry's D-ids into the D-row close of its key.

    A cited decision whose DECISION cell has a ``Closes:`` keyword that does
    not list *key* cannot be attached: the keeper would refuse the close and
    roll back the whole batch, so it is dropped and reported ``close_skipped``
    (the same pre-check the ordinary ANSWERS close gets).
    """
    info = dinfo.setdefault(key, {"dnums": set(), "sha": None, "sha_dnum": None,
                                   "conflict": False, "conflict_d": None,
                                   "superseded": [], "models": []})
    for dec in decisions:
        if view is not None and view.has_closes_keyword(dec) \
                and key not in view.closes(dec):
            plan.close_skipped.append((ident, dec, key))
            continue
        tail = dec.split("-", 1)[1] if dec.startswith("D-") else ""
        if tail.isdigit():
            info["dnums"].add(int(tail))


def _answers(plan, answers, decision_ids, by_id, pending, view=None,
             dinfo=None, owned=None):
    """One close per answered key; a key named by a router D-row (D-505) is
    owned by that pass: its cited D-ids join the D-row close instead (one
    close, all D-ids as decision evidence)."""
    merged = {}
    for ans in answers:
        merged.setdefault(ans.id, [])
        merged[ans.id] += [d for d in ans.decisions if d not in merged[ans.id]]
    for ident, decisions in merged.items():
        unknown = [d for d in decisions if d not in decision_ids]
        if unknown:
            plan.refused.append((ident, f"unknown decision {', '.join(unknown)}"))
            continue
        if ident in pending:
            item = {"key": pending[ident], "state": "open", "review_tier": "none",
                    "evidence_kinds": ["decision"], "evidence": []}
        else:
            item = _find(by_id, ident)
        if item is None:
            plan.skipped.append((ident, "no live item"))
            continue
        if dinfo is not None and item["key"] in owned:
            _contribute_drows(plan, item["key"], ident, decisions, view, dinfo)
            continue
        mismatch = _closes_mismatch(view, decisions, item["key"])
        if mismatch:
            # No ops at all for this item (not tier edit, not legacy_state,
            # not evidence, not close): otherwise the whole batch would roll
            # back on the keeper's refusal, taking every other op with it.
            name = item["id"] if "id" in item else item["key"].split(":", 1)[1]
            for dec in mismatch:
                plan.close_skipped.append((name, dec, item["key"]))
            continue
        _close(plan, item, decisions)


def _close(plan, item, decisions):
    key, st = item["key"], item["state"]
    if st in ("done", "cancelled"):
        plan.skipped.append((item["id"] if "id" in item else key.split(":", 1)[1],
                             "already " + st))
        return
    ident = key.split(":", 1)[1]
    decision_only = (item.get("review_tier") == "none"
                     and item.get("evidence_kinds") == ["decision"])
    if st == "open":
        if not decision_only:
            plan.add({"op": "edit", "key": key, "fields": {
                "review_tier": "none", "evidence_kinds": ["decision"]}})
        plan.add({"op": "legacy_state", "key": key, "state": "in_review",
                  "note": "md-sync: answered by " + ", ".join(decisions)})
    elif not decision_only:
        plan.skipped.append((ident, f"blocked: {key} is {st}"))
        return
    have = _decision_ids(item)
    for dec in decisions:
        if dec not in have:
            plan.add({"op": "add_evidence", "key": key, "kind": "decision",
                      "ref": {"id": dec}})
    plan.add({"op": "close_on_decision", "key": key})
