"""Read-only snapshot shaping and validated item filters (S1)."""
from __future__ import annotations

from datetime import datetime
import re

from .errors import Refused
from .item_fields import MODEL_FIELDS, parse_choices, parse_kg_refs
from ..views_progress import progress

FIELDS = tuple((
    "project id key title description host wait choices kg_refs waits_on_operator "
    "kind track owner state estimate_h estimate_src depends_on corrects "
    "done_criteria evidence_kinds operator priority review_tier source note epoch "
    "claimed_by worker_run reviewer_run lease_until model review_waiver reviews "
    "evidence updated_at corrected_by fixes fixed_by writer_model seat_models "
    "run_ids created_at done_at reviewed merged answer"
).split()) + tuple(MODEL_FIELDS)
BLOCKS = ("items", "aliases", "activity", "decision_text", "progress")
PARAMS = ("open", "closed", "since", "fields", "blocks")
_INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z")


def _bad(name):
    allowed = FIELDS if name == "fields" else BLOCKS if name == "blocks" else None
    msg = "snapshot " + name + " invalid"
    if allowed is not None:
        msg += "; allowed: " + ",".join(allowed)
    raise Refused("validation", msg)


def _instant(value):
    stamp = value[:-1]
    if "." in stamp:
        whole, fraction = stamp.split(".")
        stamp = whole + "." + fraction.ljust(6, "0")
    return datetime.fromisoformat(stamp + "+00:00")


def _validate(params):
    for name in params:
        if name not in PARAMS:
            _bad(name)
    values = dict.fromkeys(PARAMS)
    for name, value in params.items():
        if value is None or value == "":
            continue
        if name in ("open", "closed"):
            if type(value) not in (str, int) or value not in ("1", 1):
                _bad(name)
            value = 1
        elif name == "since":
            if not isinstance(value, str) or not _INSTANT.fullmatch(value):
                _bad(name)
            try:
                _instant(value)
            except ValueError:
                _bad(name)
        else:
            if not isinstance(value, str):
                _bad(name)
            value = value.split(",")
            allowed = FIELDS if name == "fields" else BLOCKS
            if len(value) != len(set(value)) or any(v not in allowed for v in value):
                _bad(name)
        values[name] = value
    if values["open"] and values["closed"]:
        raise Refused("validation", "snapshot open and closed are mutually exclusive")
    return values


def build(store, now, **params):
    """Shape read-only filters; the S2 HTTP adapter owns repeats and fmt."""
    values = _validate(params)
    active = any(value is not None for value in values.values())
    body = _full(store, now, values if active else None)
    if not active:
        return body
    since = _instant(values["since"]) if values["since"] else None
    selected = body["items"]
    total = len(selected)
    if values["fields"] is not None:
        names = {"key", *values["fields"]}
        selected = [{k: v for k, v in item.items() if k in names} for item in selected]
    body["items"] = selected
    if since is not None:
        body["activity"] = [entry for entry in body["activity"]
                            if _instant(entry["ts"]) >= since]
    if values["blocks"] is not None:
        for name in BLOCKS:
            if name not in values["blocks"]:
                del body[name]
    body["filter"] = {**values, "total": total, "returned": len(body.get("items", []))}
    return body


PROGRESS_FIELDS = tuple((
    "key title project kind state track owner claimed_by writer_model operator "
    "estimate_h updated_at done_at created_at"
).split())


def _selected(item, done_at, values):
    if values is None:
        return True
    terminal = item["state"] in ("done", "cancelled")
    if (values["open"] and terminal) or (values["closed"] and not terminal):
        return False
    stamps = [stamp for stamp in (item["updated_at"], done_at) if stamp]
    return not values["since"] or bool(stamps) and max(map(_instant, stamps)) >= _instant(values["since"])


def _full(store, now, values=None):
    from .store_read import _decode_models, _decode_run_ids
    items = store._all_items()
    corrected, fixed = {}, {}
    for item in items:
        if item["corrects"]:
            corrected.setdefault(item["corrects"], []).append(item["key"])
        if item.get("fixes"):
            fixed.setdefault(item["fixes"], []).append(item["key"])
    created_ts, done_ts, reviewed_keys, merged_keys = store._snapshot_run_meta()
    answers = store.answer_map()   # SYNC-MD-IMPORT: the page's Answers list
    evidence = store._evidence_by_key()      # set-based: one query each, not one per item
    verdicts = store._verdicts_by_key()
    progress_items = [dict({name: item.get(name) for name in PROGRESS_FIELDS},
                           created_at=item.get("created_at") or created_ts.get(item["key"]),
                           done_at=done_ts.get(item["key"])) for item in items]
    selected = [item for item in items if _selected(item, done_ts.get(item["key"]), values)]
    out = []
    for item in selected:
        reviews = [{"reviewer": v.get("reviewer"), "family": v.get("family")}
                   for v in store._current_round(item, verdicts.get(item["key"], []))]
        out.append({
            "project": item["project"], "id": item["id"], "key": item["key"],
            "title": item["title"], "description": item["description"],
            "host": item["host"], "wait": item.get("wait"),
            **{name: item.get(name) for name in MODEL_FIELDS},
            "choices": parse_choices(item.get("choices")),
            "kg_refs": parse_kg_refs(item.get("kg_refs")),
            "waits_on_operator": bool(item.get("operator"))
            and item.get("state") not in ("done", "cancelled"),
            "kind": item["kind"], "track": item["track"],
            "owner": item["owner"], "state": item["state"],
            "estimate_h": item["estimate_h"], "estimate_src": item["estimate_src"],
            "depends_on": item["depends_on"], "corrects": item["corrects"],
            "done_criteria": item["done_criteria"],
            "evidence_kinds": item["evidence_kinds"],
            "operator": item["operator"], "priority": item["priority"],
            "review_tier": item["review_tier"],
            "source": item["source"], "note": item["note"],
            "epoch": item["epoch"], "claimed_by": item["claimed_by"],
            "worker_run": None, "reviewer_run": None,
            "lease_until": item["lease_until"], "model": item["model"],
            "review_waiver": item.get("review_waiver"), "reviews": reviews,
            "evidence": [{"kind": r["kind"], "ref": r["ref"], "epoch": r["epoch"],
                          "verified": r["verified"], "at": r["at"]}
                         for r in evidence.get(item["key"], [])],
            "updated_at": item["updated_at"],
            "corrected_by": sorted(corrected.get(item["key"], [])),
            "fixes": item.get("fixes"),
            "fixed_by": sorted(fixed.get(item["key"], [])),
            "writer_model": item.get("writer_model"),
            "seat_models": _decode_models(item.get("seat_models")),
            "run_ids": _decode_run_ids(item.get("run_ids")),
            "created_at": item.get("created_at")
            or created_ts.get(item["key"]),
            "done_at": done_ts.get(item["key"]),
            "reviewed": item["key"] in reviewed_keys,
            "merged": item["key"] in merged_keys,
            "answer": ({"text": answers[item["key"]]["text"],
                        "decision_id": answers[item["key"]]["decision_id"]}
                       if item["key"] in answers else None),
        })
    head = store.conn.execute(
        "SELECT hash, seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return {"generated": store._iso(now), "head": head[0] if head else None,
            "head_seq": head[1] if head else 0,   # PG-LOCAL-COPY: readers refuse an older copy
            "items": out,
            "aliases": {a["old"]: a["new"] for a in store._aliases()},
            "activity": store._activity(now),
            "decision_text": store._decision_text(selected, now, evidence),
            "progress": progress(progress_items)}   # PROGRESS-PANEL: the page's static panel
