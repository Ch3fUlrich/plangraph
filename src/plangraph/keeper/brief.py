"""Short task briefs: one capped block per item (PG-BRIEF, D-708 A).

Spawners and agents pull a brief by key instead of rewriting briefs.  A brief
carries only what an agent needs to start — identity, state, requirements and
the latest note line — never events, evidence rows, reviews or aliases.

The rendered text is capped at ``BRIEF_CAP`` chars (~400 tokens).  Only
``description`` (first), ``done_criteria`` and finally the note line may
shrink to fit; each cut is marked `` …[+N chars]``.  Key, title, state and
deps are never truncated.
"""
from __future__ import annotations

import json

__all__ = ["BRIEF_CAP", "BRIEF_WARNING", "brief_requested",
           "latest_note_line", "build", "brief_text", "ensure_cap", "show"]

BRIEF_CAP = 1600
BRIEF_WARNING = "description missing: brief will be empty"


def brief_requested(value) -> bool:
    """Is an HTTP/MCP ``brief`` argument asking for the brief form?"""
    return str(value if value is not None else "").strip().lower() in (
        "1", "true", "yes")


def latest_note_line(note):
    """The latest (last non-blank) line of a ``note`` column, or ``None``."""
    if not isinstance(note, str) or not note.strip():
        return None
    lines = [line.strip() for line in note.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _evidence_kinds(item) -> list:
    kinds = item.get("evidence_kinds")
    if isinstance(kinds, str):
        try:
            kinds = json.loads(kinds)
        except ValueError:
            kinds = []
    return list(kinds or [])


def build(item, deps=None) -> dict:
    """A brief dict for a store/show/snapshot-shaped *item*.

    *deps* is the ``depends_on`` list in either shape (key strings as stored,
    or ``{"key", "state"}`` mappings as shown); each entry keeps its state.
    """
    if deps is None:
        deps = item.get("depends_on") or []
    brief = {
        "key": item.get("key"),
        "title": item.get("title"),
        "state": item.get("state"),
        "kind": item.get("kind"),
        "owner": item.get("owner"),
        "review_tier": item.get("review_tier"),
        "evidence_kinds": _evidence_kinds(item),
        "priority": item.get("priority", 0),
        "depends_on": [
            {"key": entry.get("key"), "state": entry.get("state")}
            if isinstance(entry, dict) else {"key": entry, "state": None}
            for entry in deps],
        "description": item.get("description"),
        "done_criteria": item.get("done_criteria"),
    }
    if item.get("wait"):
        brief["wait"] = item.get("wait")
    note = latest_note_line(item.get("note"))
    if note:
        brief["note"] = note
    return ensure_cap(brief)


def brief_text(brief: dict) -> str:
    """The compact text form of a brief dict (what ``--brief`` prints)."""
    kinds = ",".join(brief.get("evidence_kinds") or []) or "-"
    lines = [
        f"{brief.get('key')} | {brief.get('title') or ''}",
        f"state {brief.get('state')} | kind {brief.get('kind')} | "
        f"owner {brief.get('owner') or '-'} | tier {brief.get('review_tier')} | "
        f"prio {brief.get('priority', 0)} | evidence {kinds}",
    ]
    if brief.get("wait"):
        lines.append(f"wait {brief['wait']}")
    deps = ", ".join(f"{entry.get('key')}={entry.get('state')}"
                     for entry in brief.get("depends_on") or []) or "-"
    lines.append(f"deps {deps}")
    if brief.get("description"):
        lines.append(f"desc {brief['description']}")
    if brief.get("done_criteria"):
        lines.append(f"done {brief['done_criteria']}")
    if brief.get("note"):
        lines.append(f"note {brief['note']}")
    return "\n".join(lines)


def _size(brief: dict) -> int:
    """The chars the text door would send for *brief* (the capped artifact)."""
    return len(brief_text(brief))


def _cut(value: str, save: int) -> str:
    """Shorten *value* by at least *save* chars, marking the cut.

    Returns ``""`` when even an empty cut cannot save enough (the caller
    then drops the field's line entirely).
    """
    original = len(value)
    if original == 0 or save <= 0:
        return value
    for kept in range(original - 1, -1, -1):
        marker = f" …[+{original - kept} chars]"
        if len(value[:kept] + marker) <= original - save:
            return value[:kept] + marker
    return ""


def ensure_cap(brief: dict) -> dict:
    """Shrink ``description``, then ``done_criteria``, then ``note`` to fit.

    A field that cannot cover the need on its own is left intact while a
    later field can; whole lines are shed only as a last resort.  Key,
    title, state and deps are never touched.
    """
    for field in ("description", "done_criteria", "note"):
        if _size(brief) <= BRIEF_CAP:
            break
        value = brief.get(field) or ""
        if not value:
            continue
        cut = _cut(value, _size(brief) - BRIEF_CAP)
        if cut:
            brief[field] = cut
    if _size(brief) <= BRIEF_CAP:
        return brief
    for field in ("description", "done_criteria", "note"):
        if _size(brief) <= BRIEF_CAP:
            break
        if not brief.get(field):
            continue
        cut = _cut(brief[field], _size(brief) - BRIEF_CAP)
        if cut:
            brief[field] = cut
        else:
            brief.pop(field, None)
    return brief


def show(store, principal, key, raw, as_text, text_fn):
    """Brief-aware ``Store.show`` for the API's ``show`` read branch.

    Returns the ``(payload, as_text)`` pair the handler sends, using
    :func:`brief_text` for a brief and *text_fn* otherwise.
    """
    want = brief_requested(raw)
    info = store.show(principal, key, brief=want)
    render = brief_text if want else text_fn
    return (render(info), True) if as_text else (info, False)
