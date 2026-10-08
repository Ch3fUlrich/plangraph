"""YAML directory <-> :meth:`Store.import_items`, plus round-trip export.

The importer is the only keeper module that touches YAML, so PyYAML is imported
here alone (``docs/KEEPER.md``).  ``load_dir`` merges every ``*.yaml`` file in a
directory into one router-attributed batch for ``Store.import_items``: it adds
the global ``project:id`` key, qualifies bare dependency ids, folds
provenance-only helper fields into ``note`` and refuses duplicate keys, wrong
field types and any state other than ``open``/``claimed``/``in_review`` (``done``
and ``cancelled`` are never importable).  ``export_yaml`` writes one block-style
file per track from the store's current rows; ``load_dir(export_yaml(...))``
reproduces the batch exactly.

Both functions work on a plain connection or a :class:`Store` and need no
running server.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from .errors import Refused
from .note_fold import DESCRIPTION_CAP, fold_checked as _fold_note
from .item_fields import KIND_DEFAULTS, check_host, check_kind, check_wait, kind_defaults
from .store_create import default_review_tier, validated_review_tier

__all__ = ["load_dir", "export_yaml"]

#: The item fields the importer emits and the exporter writes, in output order.
_FIELD_ORDER = (
    "project", "id", "key", "title", "description", "host", "wait", "kind", "track",
    "owner", "state",
    "estimate_h", "estimate_src", "depends_on", "corrects", "done_criteria",
    "evidence_kinds", "operator", "priority", "review_tier", "source", "note",
)
_FIELDS = frozenset(_FIELD_ORDER)
_IMPORT_STATES = ("open", "claimed", "in_review")
REQUIRED = ("project", "id")


def _bad(key, msg):
    return Refused("validation", f"{key}: {msg}")


def _qualify(project, dep):
    """A bare id means "same project"; a ``project:id`` is already global."""
    return dep if ":" in dep else f"{project}:{dep}"


def _string(raw, field, key):
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bad(key, f"{field} must be a string")
    return value


def _string_list(raw, field, key):
    value = raw.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise _bad(key, f"{field} must be a list")
    for entry in value:
        if not isinstance(entry, str) or not entry:
            raise _bad(key, f"{field} entries must be non-empty strings")
    return list(value)


def _normalise_item(raw, where):
    if not isinstance(raw, dict):
        raise Refused("validation", f"{where}: each item must be a mapping")

    project = raw.get("project")
    if not isinstance(project, str) or not project:
        raise Refused("validation", f"{where}: item needs a project string")
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise Refused("validation",
                      f"{where}: item in {project!r} needs an id string")
    key = f"{project}:{identifier}"
    declared = raw.get("key")
    if declared is not None and declared != key:
        raise _bad(key, f"key {declared!r} != {key!r}")

    kind = check_kind(raw.get("kind", "lane"), key)
    state = raw.get("state", "open")
    if state not in _IMPORT_STATES:
        raise _bad(key, f"state {state!r} is not importable")

    estimate = raw.get("estimate_h")
    if estimate is not None and (isinstance(estimate, bool)
                                 or not isinstance(estimate, (int, float))):
        raise _bad(key, "estimate_h must be a number")
    priority = raw.get("priority", 0)
    if priority is None:
        priority = 0
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise _bad(key, "priority must be an integer")
    operator = raw.get("operator")
    if operator is not None and not isinstance(operator, bool):
        raise _bad(key, "operator must be a boolean")
    operator, wait_raw = kind_defaults(raw, kind)

    depends = sorted(_qualify(project, dep)
                     for dep in _string_list(raw, "depends_on", key))
    corrects = raw.get("corrects")
    if corrects in (None, ""):
        corrects = None
    elif not isinstance(corrects, str):
        raise _bad(key, "corrects must be a string")
    else:
        corrects = _qualify(project, corrects)
    kinds = _string_list(raw, "evidence_kinds", key) or ["any"]

    title = _string(raw, "title", key) or ""
    host = check_host(_string(raw, "host", key), key)
    wait = check_wait(wait_raw, key)
    description = _string(raw, "description", key)
    if description is not None and len(description) > DESCRIPTION_CAP:
        raise _bad(key, f"description exceeds {DESCRIPTION_CAP} characters")
    track = _string(raw, "track", key)
    explicit_tier = raw.get("review_tier")
    if explicit_tier is not None:
        review_tier = validated_review_tier(explicit_tier)
    else:
        review_tier = default_review_tier(project, identifier, title, track)

    unknown = {name: raw[name] for name in raw if name not in _FIELDS}
    note = _fold_note(raw.get("note"), unknown, key)

    values = {
        "project": project, "id": identifier, "key": key,
        "title": title, "description": description, "host": host,
        "wait": wait,
        "kind": kind, "track": track,
        "owner": _string(raw, "owner", key), "state": state,
        "estimate_h": estimate, "estimate_src": _string(raw, "estimate_src", key),
        "depends_on": depends, "corrects": corrects,
        "done_criteria": _string(raw, "done_criteria", key),
        "evidence_kinds": kinds, "operator": operator, "priority": priority,
        "review_tier": review_tier,
        "source": _string(raw, "source", key), "note": note,
    }
    return {field: values[field] for field in _FIELD_ORDER}


def load_dir(path):
    """Read every ``*.yaml`` under *path* into one sorted batch of items."""
    root = Path(path)
    if not root.is_dir():
        raise Refused("validation", f"not a directory: {root}")
    files = sorted(set(root.glob("*.yaml")) | set(root.glob("*.yml")))
    items = []
    seen = {}
    for file in files:
        try:
            data = yaml.safe_load(file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise Refused("validation", f"{file.name}: bad YAML: {exc}") from exc
        if data is None:
            continue
        if not isinstance(data, dict):
            raise Refused("validation",
                          f"{file.name}: top level must be a mapping")
        raw_items = data.get("items")
        if raw_items is None:
            continue
        if not isinstance(raw_items, list):
            raise Refused("validation", f"{file.name}: items must be a list")
        for raw in raw_items:
            item = _normalise_item(raw, file.name)
            key = item["key"]
            if key in seen:
                raise Refused("validation",
                              f"duplicate key {key} in {seen[key]} and "
                              f"{file.name}")
            seen[key] = file.name
            items.append(item)
    return sorted(items, key=lambda item: item["key"])


# ------------------------------------------------------------------ export

def _read_items(source):
    """Store rows in definition shape, from a Store or a raw connection."""
    if hasattr(source, "_all_items"):
        return source._all_items()
    deps = {}
    for row in source.execute("SELECT src,dst FROM edges"):
        deps.setdefault(row["src"], []).append(row["dst"])
    out = []
    for row in source.execute("SELECT * FROM items ORDER BY key"):
        item = dict(row)
        try:
            kinds = json.loads(item.get("evidence_kinds") or "[]")
        except (ValueError, TypeError):
            kinds = []
        item["evidence_kinds"] = kinds
        item["operator"] = bool(item.get("operator"))
        item["depends_on"] = sorted(deps.get(item["key"], []))
        out.append(item)
    return out


def _export_record(item):
    record = {}
    for field in _FIELD_ORDER:
        value = item.get(field)
        if value is None and not (field == "wait" and item.get("kind") in KIND_DEFAULTS):
            continue  # a kind with a default wait keeps an explicit null
        if field == "depends_on":
            if not value:
                continue
            value = sorted(value)
        elif field == "evidence_kinds":
            value = list(value)
        elif field == "estimate_h" and isinstance(value, float) \
                and value.is_integer():
            value = int(value)  # SQLite REAL columns coerce whole numbers
        record[field] = value
    return record


def _slug(track):
    return "".join(char if char.isalnum() or char in "-_" else "_"
                   for char in track)


def export_yaml(conn_or_store, dir):
    """Write one block-style YAML file per track; returns the paths written.

    Only importable states are exported (``open``/``claimed``/``in_review``):
    ``done`` and ``cancelled`` are never accepted by :func:`load_dir`, so
    emitting them would make the export impossible to round-trip.  Ordering is
    fully deterministic — tracks sorted, items sorted by key.
    """
    out = Path(dir)
    out.mkdir(parents=True, exist_ok=True)
    groups = {}
    for item in _read_items(conn_or_store):
        if item.get("state") not in _IMPORT_STATES:
            continue
        track = item.get("track") or "untracked"
        groups.setdefault(str(track), []).append(item)
    written = []
    for track in sorted(groups):
        records = [_export_record(item)
                   for item in sorted(groups[track], key=lambda it: it["key"])]
        text = yaml.safe_dump({"items": records}, sort_keys=False,
                              default_flow_style=False, allow_unicode=True,
                              width=200)
        path = out / f"{_slug(track)}.yaml"
        path.write_text(text, encoding="utf-8")
        written.append(str(path))
    return written
