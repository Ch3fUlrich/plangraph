"""RT-OVERSIGHT-KEEPER-SCOPE: the pure authorisation policy for oversight.

The narrow grant in one place: an ``oversight`` principal may use exactly one
write verb (``apply_ops``) and, inside it, exactly four op kinds --

* ``create`` a ``lane``/``question`` with an EXPLICIT tier (``standard``, or
  ``none`` only for a question) and non-empty ``done_criteria``.  The tier is
  raise-only: a tier below the project default is refused
  ``oversight:tier_below_default`` (never silently raised to the default); a
  tier at or above the default is honoured;
* ``edit`` only ``note``/``description``/``depends_on`` on an OPEN, non-high,
  non-gate/goal/decision/operator_task item, and only ADD dependencies;
* ``priority`` on such an item;
* ``add_dep`` on such an item, to a dependency that already exists and is not
  cancelled (a DONE target is legal; the kind/tier bars still apply).

R5 bounds every one of those ops to the principal's ``projects``: the item a
``create``/``edit``/``priority``/``add_dep`` names, the dependency an
``add_dep``/``create``/``edit`` adds, and every dependency target must be inside
the grant.  An empty project list allows nothing (fail closed).

Everything is decided from database rows (``items_by_key``), never from the
caller's reason/finding inputs.  Unknown ops, unknown op fields, unknown item
fields and unknown dependencies are denied: the policy fails closed and never
defaults an unrecognised name to "allow".

No I/O and no clock: the store calls :func:`check_batch` twice (pre-lock and
in-lock) and :func:`check_action` at the verb seam, so both seams share exactly
one implementation.

Router rulings pinned here: R1 (no tier lowering by oversight beyond the
explicit create-time choice; ``none`` only for ``question``), R6 (``decision``
and ``operator_task`` are forbidden dependency targets too).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import api_routes
from .errors import Refused

__all__ = ["Verdict", "check_action", "check_batch", "ALLOWED_ACTION",
           "OVERSIGHT_OPS", "OVERSIGHT_CREATE_FIELDS", "OVERSIGHT_EDIT_FIELDS",
           "FORBIDDEN_TARGET_KINDS", "OVERSIGHT_APPLIED_PER_HOUR",
           "OVERSIGHT_DENIED_PER_HOUR", "OVERSIGHT_CREATE_KINDS"]

#: The only write verb reachable by an oversight principal.
ALLOWED_ACTION = "apply_ops"

#: Ops the policy understands.  A name outside this set is denied, not passed
#: through to ``_run_op``.
OVERSIGHT_OPS = frozenset({"create", "edit", "priority", "add_dep"})

#: The closed create allowlist.  ``under`` is deliberately absent (it writes a
#: gate edge and an ``item_defined`` on the gate).
OVERSIGHT_CREATE_FIELDS = frozenset({
    "project", "id", "kind", "title", "description", "note", "done_criteria",
    "review_tier", "evidence_kinds", "depends_on", "priority", "owner",
})

#: Edits may only touch the prose and add edges; ``done_criteria`` and every
#: tier/lifecycle field is refused.
OVERSIGHT_EDIT_FIELDS = frozenset({"note", "description", "depends_on"})

#: R6: gate/goal are named by the item; decision/operator_task are the inbox's
#: own graph and are refused too.
FORBIDDEN_TARGET_KINDS = frozenset({"gate", "goal", "decision", "operator_task"})

OVERSIGHT_CREATE_KINDS = frozenset({"lane", "question"})

#: Derived rate limits (no counter table): counted from ``oversight_ops``.
OVERSIGHT_APPLIED_PER_HOUR = 120
OVERSIGHT_DENIED_PER_HOUR = 10

_DECISION_EVIDENCE = (frozenset({"decision"}), frozenset({"decision", "commit"}))

#: The exception family malformed op data raises; ``check_batch`` turns each
#: one into a fail-closed ``malformed`` verdict (same set as ``store_ops``).
_MALFORMED = (TypeError, ValueError, AttributeError, KeyError)

#: Refusal code per reason.  The public ``error`` stays a contract code; the
#: reason rides in the message as ``oversight:<reason>``.
_CODES = {
    "verb_denied": "forbidden",
    "op_denied": "forbidden",
    "field_denied": "forbidden",
    "field_unknown": "validation",
    "kind_denied": "forbidden",
    "tier_denied": "validation",
    "criteria_missing": "validation",
    "tier_below_default": "forbidden",
    "malformed": "validation",
    "target_high": "forbidden",
    "target_kind": "forbidden",
    "target_unknown": "not_found",
    "bad_state": "bad_state",
    "dep_removal": "forbidden",
    "dep_unknown": "validation",
    "dep_cancelled": "validation",
    "batch_ref_required": "validation",
    "rate_limited": "forbidden",
    "project_denied": "forbidden",
}


@dataclass(frozen=True)
class Verdict:
    """One refusal the store turns into ``Refused`` (and an audit row)."""

    index: int
    op: str
    key: str
    code: str
    reason: str
    finding: str = ""

    @property
    def message(self) -> str:
        where = self.key or "-"
        return f"oversight:{self.reason} (op {self.index + 1} {self.op} {where})"


def check_action(action) -> None:
    """Allow the one write verb and every known read; deny everything else.

    A non-text action (an unhashable ``list``/``dict`` name) fails closed as a
    denial instead of raising ``TypeError``: malformed input never 500s.
    """
    if action == ALLOWED_ACTION:
        return
    if not isinstance(action, str):
        raise Refused("forbidden",
                      f"oversight:verb_denied {str(action)!r} is not granted")
    # WRITE_VERBS wins where a name is both (``alarms`` is an inbox read and a
    # worker write); a write the policy does not grant is always denied.
    if action in api_routes.WRITE_VERBS:
        raise Refused("forbidden", f"oversight:verb_denied {action!r} is not granted")
    if action in api_routes.READ_VERBS:
        return
    raise Refused("forbidden", f"oversight:verb_denied {action!r} is not granted")


def _verdict(index, op, key, reason):
    return Verdict(index, op, key, _CODES.get(reason, "forbidden"), reason)


def _key_of(op):
    if op.get("op") == "create":
        item = op.get("item")
        if isinstance(item, dict) and item.get("project") and item.get("id"):
            return f"{item['project']}:{item['id']}"
        return "-"
    return str(op.get("key") or "-")


def _item_reason(item):
    """Why *item* may not be an oversight target/dependency (``None`` = ok).

    Kind is checked before tier: a high gate must report ``target_kind`` (the
    more specific denial), not ``target_high``.
    """
    if item is None:
        return "target_unknown"
    kind = item.get("kind") or "lane"
    if kind in FORBIDDEN_TARGET_KINDS:
        return "target_kind"
    tier = item.get("review_tier") or "standard"
    if tier == "high":
        return "target_high"
    if item.get("state") not in (None, "open"):
        return "bad_state"
    return None


def _dependency_reason(dep, items):
    """Why *dep* may not be added (``None`` = ok).

    The dependency grant is "an existing non-cancelled dependency" (contract,
    ``docs/KEEPER.md``): only ``dep_unknown``/``dep_cancelled`` are state
    denials here, so a DONE or claimed target is legal.  R1/R6 still bar a
    ``high`` tier and a gate/goal/decision/operator_task target, while the
    ITEM the edit/priority lands on keeps the OPEN check through
    :func:`_item_reason`.
    """
    target = items.get(dep)
    if target is None:
        return "dep_unknown"
    if target.get("state") == "cancelled":
        return "dep_cancelled"
    kind = target.get("kind") or "lane"
    if kind in FORBIDDEN_TARGET_KINDS:
        return "target_kind"
    if (target.get("review_tier") or "standard") == "high":
        return "target_high"
    return None


def _project_of(items, key):
    """The project *key* belongs to: the row's column, else the key prefix."""
    item = items.get(key)
    if isinstance(item, dict) and isinstance(item.get("project"), str):
        return item["project"]
    if isinstance(key, str) and ":" in key:
        return key.split(":", 1)[0]
    return ""


def _scope_reason(items, key, projects):
    """R5: ``project_denied`` unless *key* lives in the granted *projects*."""
    return None if _project_of(items, key) in projects else "project_denied"


def _create_scope_reason(raw, projects):
    """R5: the item a create would insert must be inside the grant."""
    project = raw.get("project")
    return None if isinstance(project, str) and project in projects \
        else "project_denied"


def _check_create(index, op, items, projects):
    raw = op.get("item")
    if not isinstance(raw, dict):
        return [_verdict(index, "create", _key_of(op), "op_denied")]
    key = _key_of(op)
    reason = _create_scope_reason(raw, projects)
    if reason:
        return [_verdict(index, "create", key, reason)]
    unknown = set(raw) - OVERSIGHT_CREATE_FIELDS
    if unknown:
        return [_verdict(index, "create", key, "field_unknown")]
    kind = raw.get("kind", "lane")
    if not isinstance(kind, str) or kind not in OVERSIGHT_CREATE_KINDS:
        # A non-text kind (a list, a number) is denied as a kind, never raised.
        return [_verdict(index, "create", key, "kind_denied")]
    tier = raw.get("review_tier")
    if tier is None or tier not in ("standard", "none"):
        return [_verdict(index, "create", key, "tier_denied")]
    if tier == "none":
        if kind != "question":
            return [_verdict(index, "create", key, "tier_denied")]
        kinds = raw.get("evidence_kinds")
        kinds = list(kinds) if isinstance(kinds, (list, tuple)) else []
        if not all(isinstance(k, str) for k in kinds) \
                or frozenset(kinds) not in _DECISION_EVIDENCE \
                or len(kinds) != len(set(kinds)):
            return [_verdict(index, "create", key, "tier_denied")]
    criteria = raw.get("done_criteria")
    if not isinstance(criteria, str) or not criteria.strip():
        return [_verdict(index, "create", key, "criteria_missing")]
    deps = raw.get("depends_on") or []
    if not isinstance(deps, (list, tuple)) or any(not isinstance(d, str) for d in deps):
        return [_verdict(index, "create", key, "field_unknown")]
    for dep in deps:
        reason = _dependency_reason(dep, items)
        if reason:
            return [_verdict(index, "create", key, reason)]
        reason = _scope_reason(items, dep, projects)
        if reason:
            return [_verdict(index, "create", key, reason)]
    # Raise-only tiers (R1): the last word on an otherwise legal create is the
    # project default.  It runs LAST so every specific denial above keeps its
    # own reason, and it mirrors the store's own ``creation_tier`` refusal --
    # a below-default tier is refused here, never clamped to the default.
    # ``none`` is the policy's own question carve-out, not a lowering.
    if tier != "none" and _tier_order(tier) < _tier_order(_create_default(raw)):
        return [_verdict(index, "create", key, "tier_below_default")]
    return []


def _tier_order(tier) -> int:
    """``none < standard < high``; an unknown tier orders below every tier."""
    return {"none": 0, "standard": 1, "high": 2}.get(tier, -1)


def _create_default(raw) -> str:
    """The default ``store_create.creation_tier`` computes for this raw create.

    The same three inputs the store reads (project, id, title/track when they
    are text), so the policy verdict and the store refusal never disagree on a
    create that both of them can see.
    """
    from .store_tiers import default_review_tier

    identifier, title, track = raw.get("id"), raw.get("title"), raw.get("track")
    return default_review_tier(
        str(raw.get("project") or ""),
        identifier if isinstance(identifier, str) else None,
        title if isinstance(title, str) else None,
        track if isinstance(track, str) else None)


def _deps_delta(old, value):
    """``(added, removing, valid)`` for a ``depends_on`` edit of a list *old*.

    Both request shapes of BATCH.md's ``edit`` are understood:

    * a **list** replaces the whole list -- ``added`` is what it brings,
      ``removing`` is whatever it leaves out;
    * ``{add: [...], remove: [...]}`` edits the list in place -- the ``remove``
      side is a removal even when it names nothing, so the grant never has to
      decide whether an empty side is "harmless";
    * anything else (``{}``, another key, a non-list side, a non-string key)
      is ``valid=False`` and the policy fails closed on ``field_denied``.
    """
    old = set(old or ())
    if isinstance(value, (list, tuple)):
        if any(not isinstance(d, str) for d in value):
            return set(), set(), False
        new = set(value)
        return new - old, bool(old - new), True
    if not isinstance(value, dict) or not value \
            or set(value) - {"add", "remove"}:
        return set(), set(), False
    sides = []
    for name in ("add", "remove"):
        part = value.get(name, [])
        if not isinstance(part, (list, tuple)) or any(
                not isinstance(d, str) for d in part):
            return set(), set(), False
        sides.append(set(part))
    add, remove = sides
    resulting = (old - remove) | add
    return (resulting - old), (bool(old - resulting) or "remove" in value), True


def _deps_after(item, value):
    """The item's ``depends_on`` after a GRANTED edit of *value* (either form)."""
    old = set(item.get("depends_on") or [])
    if isinstance(value, dict):
        add = set(value.get("add") or ())
        remove = set(value.get("remove") or ())
        return sorted((old - remove) | add)
    return list(value)


def _check_edit(index, op, items, projects):
    key = _key_of(op)
    fields = op.get("fields")
    if not isinstance(fields, dict) or not fields:
        return [_verdict(index, "edit", key, "field_denied")]
    if set(fields) - OVERSIGHT_EDIT_FIELDS:
        return [_verdict(index, "edit", key, "field_denied")]
    item = items.get(key)
    reason = _item_reason(item)
    if reason:
        return [_verdict(index, "edit", key, reason)]
    reason = _scope_reason(items, key, projects)
    if reason:
        return [_verdict(index, "edit", key, reason)]
    if "depends_on" not in fields:
        return []
    added, removing, valid = _deps_delta(item.get("depends_on"),
                                         fields.get("depends_on"))
    if not valid:
        return [_verdict(index, "edit", key, "field_denied")]
    if removing:
        # A ``remove`` side or a replace that leaves a dep out: the grant is
        # additions only, so both are refused as a removal.
        return [_verdict(index, "edit", key, "dep_removal")]
    if not added:
        # No-op / reordering: the grant is additions only, so fail closed.
        return [_verdict(index, "edit", key, "dep_removal")]
    for dep in sorted(added):
        dep_reason = _dependency_reason(dep, items)
        if dep_reason:
            return [_verdict(index, "edit", key, dep_reason)]
        dep_reason = _scope_reason(items, dep, projects)
        if dep_reason:
            return [_verdict(index, "edit", key, dep_reason)]
    return []


def _check_priority(index, op, items, projects):
    key = _key_of(op)
    reason = _item_reason(items.get(key))
    if reason:
        return [_verdict(index, "priority", key, reason)]
    reason = _scope_reason(items, key, projects)
    if reason:
        return [_verdict(index, "priority", key, reason)]
    return []


def _check_add_dep(index, op, items, projects):
    key = _key_of(op)
    item = items.get(key)
    reason = _item_reason(item)
    if reason:
        return [_verdict(index, "add_dep", key, reason)]
    reason = _scope_reason(items, key, projects)
    if reason:
        return [_verdict(index, "add_dep", key, reason)]
    dep = op.get("dep")
    if not isinstance(dep, str):
        return [_verdict(index, "add_dep", key, "field_denied")]
    dep_reason = _scope_reason(items, dep, projects)
    if dep_reason:
        return [_verdict(index, "add_dep", key, dep_reason)]
    if dep in (item.get("depends_on") or []):
        return []                       # already present: a no-op, as _dep_op
    dep_reason = _dependency_reason(dep, items)
    if dep_reason:
        return [_verdict(index, "add_dep", key, dep_reason)]
    return []


def check_batch(items_by_key, ops, projects=(), *, normalize_create=None):
    """Return the deny verdicts for *ops*; empty means the batch is granted.

    *items_by_key* is the graph as it is BEFORE the batch (the store passes
    ``_all_items()``), so a key created earlier in the same batch is simulated
    here -- exactly as the in-lock ops will see it.  *projects* is the
    oversight principal's verbatim ``projects`` scope (R5); an empty list
    allows nothing, so a caller that forgets it fails closed.
    """
    items = {key: dict(item, depends_on=list(item.get("depends_on") or []))
             for key, item in (items_by_key or {}).items()}
    projects = frozenset(p for p in (projects or ()) if isinstance(p, str))
    verdicts = []
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            # Malformed shape: a fail-closed verdict, never a TypeError.
            verdicts.append(_verdict(index, "-", "", "malformed"))
            continue
        name = op.get("op")
        if not isinstance(name, str):
            verdicts.append(_verdict(index, str(name), _key_of(op), "malformed"))
            continue
        try:
            if name == "remove_dep":
                # A removal is refused as a removal (never silently as "unknown op"):
                # the grant is additions only.
                verdicts.append(_verdict(index, "remove_dep", _key_of(op),
                                         "dep_removal"))
                continue
            if name not in OVERSIGHT_OPS:
                verdicts.append(_verdict(index, str(name), _key_of(op), "op_denied"))
                continue
            if name == "create":
                found = _check_create(index, op, items, projects)
                verdicts.extend(found)
                raw = op.get("item")
                if not found and isinstance(raw, dict):
                    key = _key_of(op)
                    if normalize_create is None:
                        from .store_create import oversight_definition
                        definition = oversight_definition(raw)
                    else:
                        definition = normalize_create(raw)
                    items[key] = dict(definition, state="open")
                continue
            if name == "edit":
                found = _check_edit(index, op, items, projects)
                verdicts.extend(found)
                if not found and isinstance(op.get("fields"), dict) \
                        and "depends_on" in op["fields"]:
                    items[key_of(op)]["depends_on"] = _deps_after(
                        items[key_of(op)], op["fields"]["depends_on"])
                continue
            if name == "priority":
                verdicts.extend(_check_priority(index, op, items, projects))
                continue
            found = _check_add_dep(index, op, items, projects)
            verdicts.extend(found)
            if not found:
                items[key_of(op)]["depends_on"] = list(
                    (items[key_of(op)].get("depends_on") or []) + [op.get("dep")])
        except _MALFORMED:
            # An unhashable or oddly-typed value inside an op (a list ``kind``,
            # ``evidence_kinds=[{}]``, a dict where a name belongs) is
            # malformed input: one fail-closed verdict, never an exception a
            # door would turn into a 500.
            verdicts.append(_verdict(index, name, _key_of(op), "malformed"))
    return verdicts


def key_of(op):
    """Public helper: the item key an op names (``-`` when it names none)."""
    return _key_of(op)
