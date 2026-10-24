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
from plangraph.keeper.cli_local import resolve_alias  # noqa: E402
from plangraph.keeper.evidence_decisions import DecisionsAdapter  # noqa: E402
from plangraph.keeper.item_fields import CHOICE_KINDS  # noqa: E402

import md_sync_dclose  # noqa: E402  (D-505: router D-rows with Closes key@sha)

# Defaults only: build_plan resolves these through the snapshot's rehome
# aliases (and an optional --gate override) onto the Plan below, so a rehomed
# gate (old key cancelled, alias old -> new) is imported into its new project.
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
        self.choices_skipped = []  # (key, state): valid Options block on a non-open item
        self.redactions = 0
        #: the resolved import target (module-constant defaults; build_plan
        #: overwrites them from the snapshot's rehome aliases / --gate)
        self.project = PROJECT
        self.gate_key = GATE_KEY
        self.gate_id = GATE_ID
        self.goal_key = GOAL_KEY

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


def _find(by_id, project, ident):
    """The live item for *ident*: *project* first, then any project."""
    found = sorted(by_id.get(ident, []), key=lambda i: i["key"])
    mine = [i for i in found if i["project"] == project]
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
               decisions_view=None, decisions_text=None, repos=None, since=1,
               aliases=None, gate=None):
    """``decisions_view`` is the keeper's own ``DecisionsAdapter`` over the
    decisions file (or ``None``: no ``Closes:`` pre-check, as before).

    With ``decisions_text`` (the raw decisions log) the router D-row pass runs
    too (D-505): its ``Closes: key@sha`` rows close only on a merged commit,
    with ``repos`` (the keeper config's ``repos``) and ``since`` (ignore D-rows
    below it) selecting the merge gate.

    ``aliases`` (the snapshot's ``old -> new`` rehome aliases) and ``gate`` (a
    ``--gate KEY`` override) resolve the import target through
    ``resolve_alias``: new lanes are created in the resolved gate's project,
    and a rehomed gate whose old key is cancelled no longer reads as closed.
    """
    plan = Plan()
    gate_key = resolve_alias(aliases or {}, gate or GATE_KEY)
    project = gate_key.split(":")[0]
    goal_key = resolve_alias(aliases or {}, f"{project}:GOAL")
    gate_id = gate_key.split(":", 1)[1]
    plan.project, plan.gate_key = project, gate_key
    plan.goal_key, plan.gate_id = goal_key, gate_id
    by_id = _index(items)
    state = {i["key"]: i for i in items}
    plan.skipped.extend((s.id, s.reason) for s in skipped_answers)
    pending = {}  # id -> key, created in this run
    new_lanes, seen = [], set()
    parent_requests = []  # (child_key, parent_str)
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
        entry_parent = getattr(entry, "parent", None)
        if isinstance(entry_parent, str):
            entry_parent = entry_parent.strip() or None
        entry_choices = getattr(entry, "choices", None)
        malformed = bool(getattr(entry, "options_malformed", False))
        item = _find(by_id, project, entry.id)
        if item is None:
            want_create = getattr(entry, "wait", None)
            # SYNC-MD-IMPORT: a new row is a MODEL-KINDS question (or operator step).
            lane = {"project": project, "id": entry.id, "title": title,
                    "kind": "operator_task" if want_create == "task" else "question",
                    "track": "QUESTIONS", "owner": "operator",
                    "operator": True, "evidence_kinds": ["decision"],
                    "review_tier": "none", "depends_on": [], "source": SOURCE}
            if want_create is not None:
                lane["wait"] = want_create
            if desc:
                lane["description"] = desc
            choices = _masked_choices(entry_choices, plan)
            if choices:
                lane["choices"] = choices
            plan.add({"op": "create", "item": lane})
            key = f"{project}:{entry.id}"
            pending[entry.id] = key
            new_lanes.append(key)
            if malformed:
                plan.warnings.append({"key": key, "decision": None,
                                      "text": "malformed Options block ignored"})
            if entry_parent:
                parent_requests.append((key, entry_parent))
        else:
            is_ours_open = (item["project"] == project and item.get("source") == SOURCE
                            and item["state"] == "open")
            fields = {}
            if is_ours_open:
                if item.get("title") != title:
                    fields["title"] = title
                current = live_description(item)
                if current != desc and (desc or item.get("description")):
                    # An empty markdown description only clears a real column: a
                    # folded note line belongs to ``migrate-descriptions``, and
                    # clearing ``note`` here would be undone by the fallback above
                    # on the very next run (no idempotence).
                    fields["description"] = desc
                want = getattr(entry, "wait", None)
                if want is not None and item.get("wait") != want:
                    # Claimed/in-review items are not editable here at all: this
                    # branch only runs for open items, so no extra guard is needed.
                    fields["wait"] = want
            if entry_choices is None:
                if malformed:
                    plan.warnings.append({"key": item["key"], "decision": None,
                                          "text": "malformed Options block ignored"})
            else:
                want_choices = _masked_choices(entry_choices, plan)
                if item["project"] == project:
                    if item["state"] == "open":
                        if item.get("kind") not in CHOICE_KINDS:
                            # A legacy kind (e.g. a pre-kinds 'lane' OS row) cannot carry choices;
                            # sending them would make the keeper refuse the whole batch.
                            if want_choices:
                                plan.warnings.append({"key": item["key"], "decision": None,
                                                      "text": f"choices ignored on kind {item.get('kind')!r}"})
                        elif (item.get("choices") or None) != want_choices:
                            fields["choices"] = want_choices
                    elif item["state"] not in ("done", "cancelled"):
                        if (item.get("choices") or None) != want_choices:
                            plan.choices_skipped.append((item["key"], item["state"]))
                    # terminal states: no op
                # foreign project: no choices op
            if fields and (is_ours_open or "choices" in fields):
                # Non-md-sync open items emit a choices-only edit; ours may
                # carry title/description/wait plus choices in one edit.
                if not is_ours_open:
                    fields = {"choices": fields["choices"]} if "choices" in fields else {}
                if fields:
                    plan.add({"op": "edit", "key": item["key"], "fields": fields})
            if entry_parent and item["state"] == "open":
                parent_requests.append((item["key"], entry_parent))
    _link_with_parents(plan, state, new_lanes, parent_requests, by_id)
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
        if (item["project"] == project and item.get("source") == SOURCE
                and item["state"] == "open" and item["id"] not in seen
                and item["id"] not in answered):
            plan.gone.append(item["key"])
    _mask_guarded(plan)
    return plan


def _masked_choices(choices, plan):
    """QA-IMPORT: the parsed choices with credential-shaped text masked (never refused)."""
    if not choices:
        return None
    out = {"options": [], "default": choices.get("default"),
           "deadline": choices.get("deadline"), "blocks": None}
    for opt in choices.get("options") or []:
        entry = {"id": opt["id"]}
        for name in ("label", "consequence"):
            if opt.get(name):
                text, n = mp.redact(opt[name])
                text, m = credential_guard.mask(text)
                plan.redactions += n + m
                entry[name] = text
        if entry.get("label") != opt.get("label") or credential_guard.find_shapes(opt["id"]):
            # the id is a slug of the label: a masked label means a secret-derived id
            taken = {o["id"] for o in choices.get("options") or []} | {o["id"] for o in out["options"]}
            n = 1
            while f"masked-{n}" in taken:
                n += 1
            entry["id"] = f"masked-{n}"
            if out["default"] == opt["id"]:
                out["default"] = entry["id"]
        out["options"].append(entry)
    if choices.get("blocks"):
        text, n = mp.redact(choices["blocks"])
        text, m = credential_guard.mask(text)
        plan.redactions += n + m
        out["blocks"] = text
    return out


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


def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _resolve_parent(by_id, state, project, parent_str):
    """The live parent item for a Parent value, or None."""
    p = (parent_str or "").strip()
    if not p:
        return None
    if ":" in p:
        return state.get(p)
    found = _find(by_id, project, p)
    if found is None:
        try:
            norm = mp._norm_id(p)
        except Exception:
            norm = p
        if norm != p:
            found = _find(by_id, project, norm)
    return found


def _link(plan, state, new_lanes):
    """Reachability: new lanes hang off the gate; a new gate hangs off the goal."""
    _link_with_parents(plan, state, list(new_lanes), [], by_id=None)


def _link_with_parents(plan, state, new_lanes, parent_requests, by_id):
    """Gate linking merged with Parent edges: plan.gate_key gets one edit."""
    gate = state.get(plan.gate_key)
    requests = list(parent_requests or [])
    # Gate-creation case: fold gate-targeted children into the new gate.
    if gate is None and new_lanes:
        gate_targets = {plan.gate_id, plan.gate_key}
        extra, rest = [], []
        for child, parent in requests:
            if (parent or "").strip() in gate_targets:
                if child not in new_lanes and child not in extra:
                    extra.append(child)
            else:
                rest.append((child, parent))
        goal = state.get(plan.goal_key)
        if goal is None or goal["state"] != "open":
            raise PlanError(f"{plan.goal_key} must exist and be open to link "
                            f"{plan.gate_key}")
        plan.add({"op": "create", "item": {
            "project": plan.project, "id": plan.gate_id,
            "title": "Operator questions and steps",
            "kind": "gate", "track": "QUESTIONS", "owner": "operator",
            "depends_on": _dedup(list(new_lanes) + extra), "source": SOURCE}})
        plan.add({"op": "edit", "key": plan.goal_key, "fields": {
            "depends_on": list(goal.get("depends_on") or []) + [plan.gate_key]}})
        requests = rest
        if by_id is None:
            return
        edges = _parent_edges(plan, requests, by_id, state)
        _emit_parent_edits(plan, state, edges)
        return
    if gate is None:
        if new_lanes:
            goal = state.get(plan.goal_key)
            if goal is None or goal["state"] != "open":
                raise PlanError(f"{plan.goal_key} must exist and be open to link "
                                f"{plan.gate_key}")
            plan.add({"op": "create", "item": {
                "project": plan.project, "id": plan.gate_id,
                "title": "Operator questions and steps",
                "kind": "gate", "track": "QUESTIONS", "owner": "operator",
                "depends_on": list(new_lanes), "source": SOURCE}})
            plan.add({"op": "edit", "key": plan.goal_key, "fields": {
                "depends_on": list(goal.get("depends_on") or []) + [plan.gate_key]}})
        if by_id is None:
            return
        edges = _parent_edges(plan, requests, by_id, state)
        _emit_parent_edits(plan, state, edges)
        return
    # Gate exists.
    if new_lanes and gate["state"] != "open":
        raise PlanError(f"{plan.gate_key} is {gate['state']}: "
                        "new lanes would be unreachable")
    edges = _parent_edges(plan, requests, by_id, state) if by_id is not None else {}
    live_gate = list(gate.get("depends_on") or [])
    desired_gate = _dedup(live_gate + list(new_lanes) + list(edges.pop(plan.gate_key, [])))
    # A closed (done/cancelled) gate never gains lanes: emit no edit on it
    # (the keeper would refuse an edit to a non-open item and roll the batch).
    if desired_gate != live_gate and gate["state"] == "open":
        plan.add({"op": "edit", "key": plan.gate_key,
                  "fields": {"depends_on": desired_gate}})
    _emit_parent_edits(plan, state, edges)


def _parent_edges(plan, requests, by_id, state):
    """Resolve Parent requests to ``{parent_key: [child keys]}``; warn on bad ones."""
    edges = {}
    for child_key, parent_str in requests:
        p = (parent_str or "").strip()
        if not p:
            continue
        live_parent = _resolve_parent(by_id, state, plan.project, p)
        if live_parent is None:
            plan.warnings.append({"key": child_key, "decision": None,
                                  "text": f"parent {p} unknown"})
            continue
        if live_parent.get("kind") != "gate":
            plan.warnings.append({"key": child_key, "decision": None,
                                  "text": f"parent {p} is not a gate"})
            continue
        if live_parent.get("state") != "open":
            plan.warnings.append({"key": child_key, "decision": None,
                                  "text": f"parent {p} is {live_parent.get('state')}"})
            continue
        key = live_parent["key"]
        edges.setdefault(key, [])
        if child_key not in edges[key]:
            # idempotent: already present live edges are filtered at emit time
            edges[key].append(child_key)
    return edges


def _emit_parent_edits(plan, state, edges):
    for parent_key, children in edges.items():
        live = state.get(parent_key)
        if live is None:
            continue
        current = list(live.get("depends_on") or [])
        desired = _dedup(current + [c for c in children if c not in current])
        if desired != current:
            plan.add({"op": "edit", "key": parent_key, "fields": {"depends_on": desired}})


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
            item = _find(by_id, plan.project, ident)
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
