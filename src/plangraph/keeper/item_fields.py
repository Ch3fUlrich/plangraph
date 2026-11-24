"""Validation of the small first-class item fields (MODEL-HOST, D-677).

``host`` names the machine an item runs on (``workstation``, ``example-host``).  It
is a HOST, not a project: nullable, ``[a-z0-9._-]+``, at most ``HOST_CAP`` chars.

``wait`` says what an operator-waiting item waits for: ``answer`` (a
decision/question the operator must answer), ``task`` (a manual step the
operator must do), or null.

MODEL-KINDS adds four optional model-of-work fields: ``layer`` (L1/L2/L3), ``role``
(planner/implementer/reviewer/researcher/operator), ``agent_id`` (the agent that
runs the item, <= ``AGENT_ID_CAP`` chars of ``[A-Za-z0-9._:@-]``) and ``phase_kind``
(planning/work, gate items only).

MODEL-KINDS: ``KINDS`` lists the item kinds.  ``question``, ``decision`` and
``operator_task`` wait on the operator: ``kind_defaults`` gives their create-time
``operator`` / ``wait`` defaults (explicit values always win).  MODEL-LOOP adds
``loop``: a repeatable kind that blocks dependents like a lane and needs exactly
one exit decision (a ``decision`` item depending on it).
"""
from __future__ import annotations

import re

from .errors import Refused

_HOW = re.compile(r"^[ \t]*How:[ \t]*(.*?)(?=^[ \t]*(?:Why|Done when|Checked against):|\Z)",
                  re.M | re.S)
_PATH = re.compile(r"(?<![\w./-])(?:\.\.?/)?(?:[\w.-]+/)+[\w.-]+")
_EXTERNAL = re.compile(
    r"(?:\.\./|(?:[\w.-]+/)?(?:router/bin|handoffs|plan-order/helpers)/"
    r"|(?i:AutoOS|plan-order)/(?:src|rust|tools|deploy)/)")
_LOCAL_MODULE = re.compile(r"(?:plangraph/)?(?:src/plangraph|rust|tools|deploy)/[\w./-]+")


_QUOTED_SPAN = re.compile(
    r"""(?<!\w)'(?:[^'\n]|'(?=\w))*'(?!\w)|"[^"\n]*"|`+[^`\n]*`+"""
    r"|‘(?:[^’\n]|’(?=\w))*’(?!\w)|“[^”\n]*”")
_DESIGN_DEPENDENCY = re.compile(
    r"\b(?:"
    r"(?:recover|obtain|load|retrieve)[ \t]+(?:[\w.-]+[ \t]+){0,6}(?:design|contract)\b"
    r"|(?:use|follow|read|consult)[ \t]+(?:(?:the|an?)[ \t]+)?(?:"
    r"(?:approved|canonical|versioned|v\d+)[ \t]+(?:[\w.-]+[ \t]+){0,4}(?:design|contract)\b"
    r"|(?:[\w.-]+[ \t]+){0,5}(?:design|contract)[ \t]+(?:document|file)\b))", re.I)
_DESIGN_REF = re.compile(r"(?:[A-Za-z0-9][\w.-]*:)?(?:[\w.-]+/)*[\w.-]+")


def check_design_ref(value, key):
    if value is None:
        return
    if (not isinstance(value, str) or len(value) > 240 or not _DESIGN_REF.fullmatch(value)
            or any(part in (".", "..") for part in value.split(":")[-1].split("/"))):
        raise Refused("validation", f"{key}: design_ref must name a repository-relative path")


def check_definition_inputs(item):
    """Require explicit design inputs and local targets for outside code fixes."""
    how = _HOW.search(item.get("description") or "")
    if not how:
        return
    if not item.get("design_ref") and _DESIGN_DEPENDENCY.search(_QUOTED_SPAN.sub("\n", how[1])):
        raise Refused("validation", f"{item['key']}: name the design or contract in design_ref")
    if item["project"] != "plangraph" or item["kind"] != "lane":
        return
    paths = [path.removeprefix("./") for path in _PATH.findall(how[1])]
    if any(_EXTERNAL.match(path) for path in paths) and not any(
            _LOCAL_MODULE.fullmatch(path) and ".." not in path.split("/") for path in paths):
        raise Refused("validation", f"{item['key']}: name the plangraph module or write a port item")


HOST_CAP = 100
_HOST = re.compile(r"[a-z0-9._-]+")

#: D-677: the only legal ``wait`` values (plus null).
WAIT_VALUES = ("answer", "task")


def check_host(value, key=None):
    """Return *value* if it is ``None`` or a legal host, else refuse ``validation``."""
    if value is None:
        return None
    where = f"{key}: " if key else ""
    if not isinstance(value, str):
        raise Refused("validation", f"{where}host must be a string")
    if len(value) > HOST_CAP:
        raise Refused("validation", f"{where}host exceeds {HOST_CAP} characters")
    if not _HOST.fullmatch(value):
        raise Refused("validation",
                      f"{where}host must match [a-z0-9._-]+ (lowercase, no spaces)")
    return value


def check_wait(value, key=None):
    """Return *value* if it is ``None``, ``"answer"`` or ``"task"``, else refuse."""
    if value is None:
        return None
    where = f"{key}: " if key else ""
    if value not in WAIT_VALUES:
        raise Refused("validation",
                      f"{where}wait must be one of {list(WAIT_VALUES)} or null")
    return value


#: Optional columns folded from ``item_created``/``item_defined`` payloads.
SMALL_FIELDS = ("description", "host", "wait", "layer", "role", "agent_id",
                "phase_kind", "fixes", "choices", "kg_refs", "design_ref")
#: The MODEL-KINDS subset of ``SMALL_FIELDS``.
MODEL_FIELDS = ("layer", "role", "agent_id", "phase_kind")

LAYERS = ("L1", "L2", "L3")
ROLES = ("planner", "implementer", "reviewer", "researcher", "operator")
PHASE_KINDS = ("planning", "work")
AGENT_ID_CAP = 128
_AGENT_ID = re.compile(r"[A-Za-z0-9._:@-]+")


def _enum(name, value, allowed, key):
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        where = f"{key}: " if key else ""
        raise Refused("validation",
                      f"{where}{name} must be one of {list(allowed)} or null")
    return value


def check_agent_id(value, key=None):
    """Return *value* if it is ``None`` or a legal agent id, else refuse."""
    if value is None:
        return None
    where = f"{key}: " if key else ""
    if not isinstance(value, str):
        raise Refused("validation", f"{where}agent_id must be a string")
    if len(value) > AGENT_ID_CAP:
        raise Refused("validation",
                      f"{where}agent_id exceeds {AGENT_ID_CAP} characters")
    if not _AGENT_ID.fullmatch(value):
        raise Refused("validation",
                      f"{where}agent_id must match [A-Za-z0-9._:@-]+")
    return value


def check_model_fields(fields, kind, key=None):
    """Validate the MODEL-KINDS fields present in *fields* for an item of *kind*.

    ``phase_kind`` is refused on anything but a gate.  Absent names are skipped.
    """
    if "layer" in fields:
        _enum("layer", fields["layer"], LAYERS, key)
    if "role" in fields:
        _enum("role", fields["role"], ROLES, key)
    if "agent_id" in fields:
        check_agent_id(fields["agent_id"], key)
    if "phase_kind" in fields:
        value = _enum("phase_kind", fields["phase_kind"], PHASE_KINDS, key)
        if value is not None and kind != "gate":
            where = f"{key}: " if key else ""
            raise Refused("validation",
                          f"{where}phase_kind is only allowed on gate items")


#: Item kinds.  ``question`` / ``decision`` / ``operator_task`` wait on the
#: operator; every kind blocks dependents like a lane (MODEL-LOOP: ``loop``).
KINDS = ("lane", "gate", "goal", "question", "decision", "operator_task", "loop")
#: kind -> (operator, wait) used when the definition does not say.
KIND_DEFAULTS = {"question": (True, "answer"), "decision": (True, "answer"),
                 "operator_task": (True, "task")}


def check_kind(value, key=None):
    """Return *value* if it is one of ``KINDS``, else refuse ``validation``."""
    if not isinstance(value, str) or value not in KINDS:
        where = f"{key}: " if key else ""
        raise Refused("validation", f"{where}kind {value!r} is not one of {KINDS}")
    return value


def kind_defaults(raw, kind):
    """``(operator, wait)`` for a definition *raw* of *kind*.

    A key present in *raw* wins (``wait: null`` is an explicit "no wait"); an
    ``operator`` of null counts as absent.
    """
    op_default, wait_default = KIND_DEFAULTS.get(kind, (False, None))
    operator = raw.get("operator")
    return (op_default if operator is None else operator,
            raw["wait"] if "wait" in raw else wait_default)


# --------------------------------------------------------------------------- #
# QA-IMPORT (D-865): structured choices of an operator question / task.
CHOICE_KINDS = ("question", "decision", "operator_task")
MAX_OPTIONS = 8
OPTION_ID_CAP, LABEL_CAP, CONSEQUENCE_CAP, BLOCKS_CAP = 40, 60, 160, 200
_OPTION_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def is_control(c):
    """C0, DEL, C1, and invisible format/separator characters (ZWSP, bidi marks, U+2028/9)."""
    import unicodedata
    return ord(c) < 32 or 127 <= ord(c) < 160 or unicodedata.category(c) in ("Cf", "Zl", "Zp")


def _choice_text(name, value, cap, where, key=None):
    if not isinstance(value, str) or not value.strip() or len(value) > cap:
        raise Refused("validation", f"{where}choices {name} must be 1-{cap} characters")
    if any(is_control(c) for c in value):
        raise Refused("validation", f"{where}choices {name} must not contain control characters")
    from .credential_guard import guard
    guard(f"choices {name}", value, key)
    return value.strip()


def check_choices(value, kind, key=None):
    """Canonical JSON text of ``{options, default, deadline, blocks}`` or ``None``.

    ``options``: 0..MAX_OPTIONS ``{id, label, consequence}`` (ids unique, ``[a-z0-9-]``; an
    operator task may carry only a deadline/blocks); ``default``: one option id or null; ``deadline``: ``YYYY-MM-DD`` or null;
    ``blocks``: short text or null.  Only question/decision/operator_task items.
    """
    if value is None:
        return None
    where = f"{key}: " if key else ""
    if kind not in CHOICE_KINDS:
        raise Refused("validation", f"{where}choices only on {list(CHOICE_KINDS)} items")
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise Refused("validation", f"{where}choices must be an object") from exc
    if not isinstance(value, dict) or not set(value) <= {"options", "default", "deadline", "blocks"}:
        raise Refused("validation", f"{where}choices must be an object with options/default/deadline/blocks")
    out = {"options": [], "default": None, "deadline": None, "blocks": None}
    options = value.get("options")
    options = [] if options is None else options
    if not isinstance(options, list) or len(options) > MAX_OPTIONS:
        raise Refused("validation", f"{where}choices options must be a list of <= {MAX_OPTIONS}")
    seen = set()
    for opt in options:
        if not isinstance(opt, dict) or not set(opt) <= {"id", "label", "consequence"}:
            raise Refused("validation", f"{where}choices option must be {{id, label, consequence}}")
        oid = opt.get("id")
        if not isinstance(oid, str) or len(oid) > OPTION_ID_CAP or not _OPTION_ID.fullmatch(oid) \
                or oid in seen:
            raise Refused("validation", f"{where}choices option id must be unique [a-z0-9-]+")
        seen.add(oid)
        from .credential_guard import guard
        guard("choices option id", oid, key)
        entry = {"id": oid, "label": _choice_text("label", opt.get("label"), LABEL_CAP,
                                                  where, key)}
        if opt.get("consequence") is not None:
            entry["consequence"] = _choice_text("consequence", opt["consequence"],
                                                CONSEQUENCE_CAP, where, key)
        out["options"].append(entry)
    default = value.get("default")
    if default is not None and (not isinstance(default, str) or default not in seen):
        raise Refused("validation", f"{where}choices default must be one of the option ids")
    out["default"] = default
    deadline = value.get("deadline")
    if deadline is not None:
        if not isinstance(deadline, str) or not _DATE.fullmatch(deadline):
            raise Refused("validation", f"{where}choices deadline must be YYYY-MM-DD")
        from datetime import date
        try:
            date.fromisoformat(deadline)
        except ValueError as exc:
            raise Refused("validation", f"{where}choices deadline is not a date") from exc
    out["deadline"] = deadline
    if value.get("blocks") is not None:
        out["blocks"] = _choice_text("blocks", value["blocks"], BLOCKS_CAP, where, key)
    if not out["options"] and out["default"] is None and out["deadline"] is None \
            and out["blocks"] is None:
        return None
    import json
    return json.dumps(out, sort_keys=True, separators=(",", ":"))


def parse_choices(text):
    """The stored JSON text as a dict (``None`` when absent or unreadable)."""
    if not text:
        return None
    import json
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------- #
# PG-KG-REFS (D-879): references from a plan item to Omnigraph knowledge nodes.
KG_TYPES = ("Decision", "Rule", "Convention", "Component", "Preference", "Project")
MAX_KG_REFS = 20
_KG_REF = re.compile(r"[a-z0-9][a-z0-9-]{0,62}:(?:" + "|".join(KG_TYPES) + r"):[a-z0-9][a-z0-9._-]{0,127}")


def check_kg_refs(value, key=None):
    """Canonical JSON text of a sorted, de-duplicated list of ``graph:Type:slug`` refs, or ``None``.

    Syntax only: the keeper never calls Omnigraph (the render-time export reports dangling refs).
    """
    if value is None:
        return None
    where = f"{key}: " if key else ""
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise Refused("validation", f"{where}kg_refs must be a list") from exc
    if not isinstance(value, list) or len(value) > MAX_KG_REFS:
        raise Refused("validation", f"{where}kg_refs must be a list of <= {MAX_KG_REFS} refs")
    refs = set()
    for ref in value:
        if not isinstance(ref, str) or not _KG_REF.fullmatch(ref):
            raise Refused("validation",
                          f"{where}kg_refs entries must be graph:Type:slug with Type in {list(KG_TYPES)}")
        refs.add(ref)
    if not refs:
        return None
    import json
    return json.dumps(sorted(refs), separators=(",", ":"))


def parse_kg_refs(text):
    """The stored JSON text as a list (``[]`` when absent or unreadable)."""
    if not text:
        return []
    import json
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


# ----------------------------------------------------------- near copies
#: How a caller answers a near-copy match (ITEM-SUPERSEDE-GATE).
SIMILAR_ANSWERS = ("supersedes", "depends_on", "distinct")
_WORD = re.compile(r"[a-z0-9][a-z0-9_-]{3,}")
_FILES = re.compile(r"(?m)^files:[ \t]*(.*)$")
_SIMILAR_MIN_TOKENS = 5
#: EXPERIMENT (rule 3.76): token overlap; replace by embeddings if 7 days of
#: matches shown / answered supersedes / duplicates found by hand say so.
SIMILAR_THRESHOLD = 0.6
_TERMINAL = ("done", "cancelled")


def check_similar_answers(value, key=None):
    where = f"{key}: " if key else ""
    if not isinstance(value, dict) or any(a not in SIMILAR_ANSWERS for a in value.values()):
        raise Refused("validation", f"{where}similar must map item keys to supersedes, depends_on or distinct")
    return value


def _tokens(item):
    return set(_WORD.findall(f"{item.get('title') or ''} {item.get('description') or ''}".lower()))


def _files(item):
    found = _FILES.search(item.get("note") or "")
    return {p for p in re.split(r"[\s,\[\]\"']+", found.group(1)) if "/" in p} if found else set()


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def similar_items(definition, items):
    """``[(key, score)]``: open items of the same project and kind that are near
    copies of *definition* by title, description and files (the one function
    every creation path shares)."""
    mine = _tokens(definition)
    if len(mine) < _SIMILAR_MIN_TOKENS:
        return []
    related = {definition.get("under"), definition.get("corrects"), definition.get("fixes"),
               definition.get("slice_parent"), *(definition.get("depends_on") or []),
               *(definition.get("supersedes") or [])}
    my_files, out = _files(definition), []
    for other in items:
        if (other["key"] == definition["key"] or other["key"] in related
                or other.get("state") in _TERMINAL or other["project"] != definition["project"]
                or other.get("kind") != definition.get("kind")
                or definition["key"] in (other.get("depends_on") or [])):
            continue
        score = _jaccard(mine, _tokens(other))
        their_files = _files(other)
        if my_files and their_files:
            score = 0.6 * score + 0.4 * _jaccard(my_files, their_files)
        if score >= SIMILAR_THRESHOLD:
            out.append((other["key"], round(score, 2)))
    return sorted(out, key=lambda m: (-m[1], m[0]))
