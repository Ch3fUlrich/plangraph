"""PG-CHANGELOG-GEN: the keeper's change log, generated from its events and decisions.

``build`` folds the hash-chained events after a ``--since`` point into entries
(creates, closes by review or decision, answers, cancels, keeper restarts);
``render`` prints them as stable text.  Pure functions: the output depends only
on the events, the titles and the decision texts passed in, never on the clock,
so the same log renders to the same bytes.

``--since`` is an event sequence number (entries with ``seq`` GREATER than it) or
a date / ISO datetime (entries at or after it, UTC; a bare date means 00:00Z).

A keeper restart appears only when it left an ``outage_recorded`` event (a gap
longer than the reap interval); a restart with no gap leaves no event.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .errors import Refused
from .store_support import parse_iso

__all__ = ["parse_since", "build", "render", "referenced_decisions"]

_TEXT_CAP = 100
_SEQ_DIGITS = 15      # an event seq never needs more; keeps int() away from megadigit strings
_CLOSE_VIA = {"review_passed": "review", "closed_on_decision": "decision"}


def parse_since(value):
    """``("seq", n, label)`` or ``("date", datetime, label)``; refuses anything else."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** _SEQ_DIGITS:
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise Refused("validation", "since is required: an event seq or a date")
    text = value.strip()
    if text.isascii() and text.isdigit():
        if len(text) > _SEQ_DIGITS:
            raise Refused("validation", f"since seq {text[:20]}... is too large")
        return "seq", int(text), f"seq {int(text)}"
    moment = None
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            moment = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    except ValueError:
        pass
    if moment is None:
        moment = parse_iso(text)
        if moment is not None and moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    if moment is None:
        raise Refused("validation", f"since {text!r} is neither an event seq nor a date")
    return "date", moment, text


def referenced_decisions(events):
    """Every decision id the *events* name (for the caller to look up)."""
    found = set()
    for event in events:
        payload = event.get("payload") or {}
        if event["type"] == "answered" and payload.get("decision_id"):
            found.add(payload["decision_id"])
        elif event["type"] == "closed_on_decision":
            found.update(payload.get("decision_ids") or [])
    return found


def _after(event, kind, bound):
    if kind == "seq":
        return event["seq"] > bound
    moment = parse_iso(event.get("ts"))
    return moment is not None and moment >= bound


def _decisions(ids, texts):
    return [{"id": d, "text": texts.get(d)} for d in ids]


def build(events, since, titles, decision_texts, head_seq):
    """The change log after *since* from *events* (oldest first)."""
    kind, bound, label = parse_since(since)
    entries = []
    for event in events:
        if not _after(event, kind, bound):
            continue
        payload = event.get("payload") or {}
        etype, key = event["type"], event.get("key")
        base = {"seq": event["seq"], "ts": event["ts"], "key": key,
                "title": titles.get(key), "by": event.get("actor")}
        if etype == "item_created" and key is not None:
            entry = {**base, "type": "created"}
            if payload.get("rehomed_from"):
                entry["rehomed_from"] = payload["rehomed_from"]
        elif etype in _CLOSE_VIA and key is not None:
            ids = (payload.get("decision_ids") or []) if etype == "closed_on_decision" else []
            entry = {**base, "type": "closed", "via": _CLOSE_VIA[etype],
                     "decisions": _decisions(ids, decision_texts)}
        elif etype == "answered" and key is not None:
            entry = {**base, "type": "answered", "via": "answer",
                     "decisions": _decisions([payload["decision_id"]] if payload.get("decision_id") else [],
                                             decision_texts)}
        elif etype == "cancelled" and key is not None:
            entry = {**base, "type": "cancelled", "reason": payload.get("reason") or ""}
        elif etype == "outage_recorded":
            entry = {**base, "type": "restart", "gap_s": int(payload.get("gap_s") or 0),
                     "leases": int(payload.get("leases") or 0),
                     "tokens": len(payload.get("tokens") or [])}
        else:
            continue
        entries.append(entry)
    return {"since": label, "head_seq": head_seq, "entries": entries}


def _one_line(text, cap=_TEXT_CAP):
    text = " ".join(str(text).split())
    return text if len(text) <= cap else text[:cap].rstrip() + "…"


def _decision_detail(entry):
    parts = []
    for dec in entry["decisions"]:
        ident = _one_line(dec["id"], 40)
        parts.append(f"{ident}: {_one_line(dec['text'])}" if dec.get("text") else ident)
    return "; ".join(parts)


def _detail(entry):
    kind = entry["type"]
    if kind == "created":
        extra = (f", rehomed from {_one_line(entry['rehomed_from'], 80)}"
                 if entry.get("rehomed_from") else "")
        return f"by {_one_line(entry['by'], 60)}{extra}"
    if kind == "closed":
        if entry["via"] == "review":
            return f"review by {_one_line(entry['by'], 60)}"
        return f"decision {_decision_detail(entry)}".rstrip()
    if kind == "answered":
        return _decision_detail(entry) or f"by {_one_line(entry['by'], 60)}"
    if kind == "cancelled":
        return _one_line(entry["reason"]) or "-"
    return f"down {entry['gap_s']}s | leases {entry['leases']}, tokens {entry['tokens']}"


_ORDER = ("created", "closed", "answered", "cancelled")


def render(data) -> str:
    """Stable text: header, ``## day`` groups of one line per entry, totals."""
    entries = data["entries"]
    lines = [f"changelog since {data['since']} | head #{data['head_seq']} | "
             f"{len(entries)} entries"]
    if not entries:
        lines.append("no changes")
        return "\n".join(lines)
    day = None
    counts = dict.fromkeys(_ORDER, 0)
    restarts = 0
    for entry in entries:
        if entry["ts"][:10] != day:
            day = entry["ts"][:10]
            lines.append(f"## {day}")
        if entry["type"] == "restart":
            restarts += 1
            lines.append(f"{entry['ts'][11:16]}Z #{entry['seq']} {'restart':<9} keeper | "
                         f"{_detail(entry)}")
            continue
        counts[entry["type"]] += 1
        lines.append(f"{entry['ts'][11:16]}Z #{entry['seq']} {entry['type']:<9} "
                     f"{_one_line(entry['key'], 120)} | {_one_line(entry['title'] or '', 160)} | "
                     f"{_detail(entry)}")
    lines.append("totals: " + ", ".join(f"{name} {counts[name]}" for name in _ORDER)
                 + f", restarts {restarts}")
    return "\n".join(lines)
