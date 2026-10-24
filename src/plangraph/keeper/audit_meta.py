"""PG-AUDIT-META (D-1012/D-1013): the read verb ``audit_meta``.

Two questions the keeper can answer from its own rows: which items are missing
the metadata a worker needs to start (``description``, ``estimate_h``,
``done_criteria``, ``owner``), and which claims have gone stale (no event for
the item in N days).

Pure functions: the result depends only on the rows passed in, the injected
``now`` and ``stale_days`` — never on the wall clock — so the same database
renders the same bytes.  ``write_skeleton`` is the CLI-only half: it turns the
findings into a batch file the router can fill in and replay.

Field rules (see ``docs/KEEPER.md``):

* a value counts as missing when it is NULL or blank of whitespace;
* ``estimate_h`` is missing when it is NULL **or** ``estimate_src`` is the
  default marker (``docs/SPEC.md``: the plan stated no number, the value is a
  placeholder);
* a ``gate`` or an operator kind (``question``/``decision``/``operator_task``)
  is never audited for ``estimate_h`` or ``done_criteria`` — it is not estimated
  work; every kind is audited for ``description`` and ``owner``;
* only ``open`` and ``claimed`` items are audited: ``in_review`` is with the
  reviewer, and ``done``/``cancelled`` are locked and cannot be edited.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .errors import Refused
from .store_support import parse_iso

__all__ = ["MISSING_FIELDS", "NO_ESTIMATE_KINDS", "DEFAULT_ESTIMATE_SRC",
           "DEFAULT_STALE_DAYS", "parse_stale_days", "missing_for", "build",
           "render", "write_skeleton"]

DAY_S = 86400
DEFAULT_STALE_DAYS = 3
MAX_STALE_DAYS = 9999
#: The audited fields, in output order.
MISSING_FIELDS = ("description", "estimate_h", "done_criteria", "owner")
#: Kinds ``estimate_h`` / ``done_criteria`` are not applied to (``item_fields``:
#: the operator kinds wait on an answer, and a gate is a checkpoint, not work).
NO_ESTIMATE_KINDS = frozenset({"gate", "question", "decision", "operator_task"})
#: ``docs/SPEC.md``: an estimate the plan did not state.
DEFAULT_ESTIMATE_SRC = "default"
#: The only states an audit can still act on.
AUDIT_STATES = ("open", "claimed")
_TITLE_CAP, _CELL_CAP = 120, 60
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _one_line(value, cap=_TITLE_CAP) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text[:cap]


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return _one_line(value, _CELL_CAP)


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_stale_days(value):
    """The validated ``--stale-days`` (``None`` means the default of 3)."""
    if value is None:
        return DEFAULT_STALE_DAYS
    if isinstance(value, bool):
        raise Refused("validation", "stale_days must be a whole number of days")
    text = str(value).strip()
    if not (text.isascii() and text.isdigit()):
        raise Refused("validation", f"stale_days {text[:20]!r} is not a number of days")
    days = int(text)
    if not 1 <= days <= MAX_STALE_DAYS:
        raise Refused("validation", f"stale_days must be between 1 and {MAX_STALE_DAYS}")
    return days


def _no_estimate(item) -> bool:
    if item.get("estimate_h") is None:
        return True
    return _cell(item.get("estimate_src")).strip().lower() == DEFAULT_ESTIMATE_SRC


def missing_for(item):
    """The audited fields *item* is missing, in :data:`MISSING_FIELDS` order."""
    if item.get("state") not in AUDIT_STATES:
        return []
    out = []
    if _blank(item.get("description")):
        out.append("description")
    if item.get("kind") not in NO_ESTIMATE_KINDS:
        if _no_estimate(item):
            out.append("estimate_h")
        if _blank(item.get("done_criteria")):
            out.append("done_criteria")
    if _blank(item.get("owner")):
        out.append("owner")
    return out


def _holder(item):
    """The stale-claim group: ``claimed_by``, falling back to ``owner``."""
    return (_cell(item.get("claimed_by")).strip()
            or _cell(item.get("owner")).strip() or "(none)")


def _age_days(moment, now):
    return (float(now) - moment) / DAY_S


def _action(item, merged, known, age_days, stale_days):
    """What the router should do with a stale claim: ``close-by-D-row`` when the
    work is provably on ``main``, ``release`` when nobody reachable holds it (or
    it has been quiet past 2×N days), else ``renew`` (``docs/KEEPER.md``)."""
    if item["key"] in merged:
        return "close-by-D-row"
    claimant = _cell(item.get("claimed_by")).strip()
    if not claimant or claimant not in known:
        return "release"
    if age_days > 2 * float(stale_days):
        return "release"
    return "renew"


def _stale_entry(item, last_ts, merged, known, now, stale_days):
    stamp = last_ts.get(item["key"])
    if item.get("state") != "claimed" or not stamp:
        return None
    moment = parse_iso(stamp)
    if moment is None:
        return None
    age = _age_days(moment.timestamp(), now)
    if age <= float(stale_days):
        return None
    return {"key": item["key"], "claimed_by": _cell(item.get("claimed_by")),
            "last_ts": _one_line(stamp, 40), "age_days": round(age, 1),
            "action": _action(item, merged, known, age, stale_days)}


def build(items, last_ts, merged, known, now, now_iso, stale_days):
    """The audit over *items* (already scoped to what the caller may see).

    *last_ts* maps an item key to the timestamp of its newest event, *merged*
    names the keys carrying verified commit evidence, *known* the principal
    names, and *now* / *now_iso* the injected clock.
    """
    by_project = {}
    for item in items:
        by_project.setdefault(item.get("project") or "", []).append(item)
    fields = dict.fromkeys(MISSING_FIELDS, 0)
    projects, audited, missing_items, stale_items = [], 0, 0, 0
    for name in sorted(by_project):
        rows = by_project[name]
        found = {field: [] for field in MISSING_FIELDS}
        stubs, groups = {}, {}
        active = 0
        for item in rows:
            if item.get("state") in AUDIT_STATES:
                active += 1
            missing = missing_for(item)
            for field in missing:
                found[field].append(item["key"])
            if missing:
                stubs[item["key"]] = {"key": item["key"],
                                      "title": _one_line(item.get("title")),
                                      "state": _cell(item.get("state")),
                                      "fields": missing,
                                      "current": {f: _cell(item.get(f))
                                                  for f in MISSING_FIELDS}}
            entry = _stale_entry(item, last_ts, merged, known, now, stale_days)
            if entry is not None:
                groups.setdefault(_holder(item), []).append(entry)
        for field in MISSING_FIELDS:
            fields[field] += len(found[field])
        missing_items += len(stubs)
        stale_groups = [{"holder": holder, "count": len(items_of),
                         "items": sorted(items_of, key=lambda e: e["key"])}
                        for holder, items_of in sorted(groups.items())]
        stale_items += sum(g["count"] for g in stale_groups)
        projects.append({"project": name,
                         "missing": [{"field": field, "count": len(found[field]),
                                      "keys": sorted(found[field])}
                                     for field in MISSING_FIELDS],
                         "missing_items": len(stubs),
                         "stale_groups": stale_groups,
                         "stale_items": sum(g["count"] for g in stale_groups),
                         "stubs": [stubs[key] for key in sorted(stubs)]})
        audited += active
    return {"now": now_iso, "stale_days": stale_days, "projects": projects,
            "totals": {"projects": len(projects), "audited": audited,
                       "fields": fields, "missing_items": missing_items,
                       "stale_items": stale_items}}


def render(data) -> str:
    """Stable text: header, one block per project, a totals line."""
    projects = data.get("projects") or []
    names = ", ".join(p["project"] for p in projects) or "-"
    lines = [f"audit-meta now {data['now']} | stale-days {data['stale_days']}"
             f" | projects {names}"]
    for project in projects:
        lines.append(f"{project['project']}: missing {project['missing_items']} items"
                     f" | stale {project['stale_items']} claims")
        for entry in project["missing"]:
            lines.append(f"  {entry['field']:<15}{entry['count']:>3}  "
                         f"{', '.join(entry['keys']) or '-'}")
        for group in project["stale_groups"]:
            lines.append(f"  stale {group['holder']} ({group['count']})")
            for item in group["items"]:
                lines.append(f"    {item['key']}  last {item['last_ts']}"
                             f"  age {item['age_days']}d  -> {item['action']}")
    totals = data["totals"]
    lines.append(f"totals: projects {totals['projects']} |"
                 f" audited {totals['audited']} items |"
                 f" missing {sum(totals['fields'].values())} fields over"
                 f" {totals['missing_items']} items |"
                 f" stale {totals['stale_items']} claims")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# --skeleton DIR (the CLI half; the verb only returns the data)
# --------------------------------------------------------------------------- #

def _safe_name(project) -> str:
    return _SAFE.sub("_", project) or "project"


def _stub_line(stub) -> str:
    return (f"{stub['key']} | {_one_line(stub['title'])} | state {stub['state']}"
            f" | missing {', '.join(stub['fields'])}")


def _note_text(project, data, stubs) -> str:
    safe = _safe_name(project)
    batch = f"{safe}.BATCH.yaml"
    lines = [f"# audit-meta skeleton: {project}", "",
             f"Generated by `pg audit-meta --skeleton DIR` from the keeper read",
             f"`audit-meta` (now {data['now']}, stale-days {data['stale_days']}).",
             "",
             f"Every value below is empty on purpose: it is a stub to fill in,",
             "not a proposed edit.  Complete one item at a time, then apply it:",
             "",
             f"    python3 tools/apply_batch.py {batch} --url URL --token-file PATH",
             "",
             f"`{safe}.BATCH.md` is the batch's hashed `source`: `source_sha256` in",
             f"{batch} pins the bytes of this file, so editing here makes the",
             "applier refuse the batch (exit 1) before any call.  Re-run",
             f"`pg audit-meta --skeleton DIR` to regenerate both files.",
             "",
             f"`principal: router` in the YAML is a placeholder: `apply_batch`",
             "requires the token to be a router whose name matches it.  An l1 has",
             "no batch door — it edits items with `pg define --edit` instead.",
             "",
             "An item listed as `state claimed` cannot be edited until it is",
             "released: `edit_definition` only accepts `open` items.",
             "",
             f"## findings ({len(stubs)} items)"]
    lines += [f"- {_stub_line(stub)}" for stub in stubs]
    return "\n".join(lines) + "\n"


def _batch_text(project, data, stubs, digest) -> str:
    safe = _safe_name(project)
    lines = [f"# Filled in from `pg audit-meta` (now {data['now']}).",
             "# One edit op per item, one field per line, every value empty.",
             "# The comment above each op carries the title and the item's current",
             "# fields; a field not listed under `fields:` is already filled in.",
             f"batch: audit-meta-{safe}",
             "principal: router",
             f"source: {safe}.BATCH.md",
             f"source_sha256: {digest}",
             "ops:"]
    for stub in stubs:
        current = " | ".join(f"{field}={stub['current'][field]}"
                             for field in MISSING_FIELDS)
        lines.append(f"  # {_stub_line(stub)}")
        lines.append(f"  #   current: {current}")
        lines.append("  - op: edit")
        lines.append(f"    key: {stub['key']}")
        lines.append("    fields:")
        lines += [f'      {field}: ""' for field in stub["fields"]]
    return "\n".join(lines) + "\n"


def write_skeleton(data, directory):
    """Write ``DIR/<project>.BATCH.yaml`` + its sibling note; return the paths."""
    out = Path(str(directory))
    todo = [(p["project"], p["stubs"]) for p in data.get("projects") or []
            if p.get("stubs")]
    written = []
    if not todo:
        return written
    out.mkdir(parents=True, exist_ok=True)
    for project, stubs in todo:
        safe = _safe_name(project)
        note = _note_text(project, data, stubs)
        digest = hashlib.sha256(note.encode("utf-8")).hexdigest()
        note_path = out / f"{safe}.BATCH.md"
        note_path.write_text(note, encoding="utf-8")
        batch_path = out / f"{safe}.BATCH.yaml"
        batch_path.write_text(_batch_text(project, data, stubs, digest),
                              encoding="utf-8")
        written.extend([batch_path, note_path])
    return sorted(written)
