"""Compact text renderers for keeper results.

The API's ``?fmt=text`` responses and the CLI's default output both come from
here, so an agent sees exactly the same bytes whichever door it used.  ``json``
is always the fallback; every helper only summarises what the store returned.
"""
from __future__ import annotations

from .. import views
from .brief import brief_text

__all__ = [
    "ready_text", "show_text", "brief_text", "blocked_text", "alerts_text",
    "waves_text", "critical_text", "percent_text", "digest_text",
    "whoami_text", "snapshot_text", "result_text",
]


def ready_text(items, project=None, n=10, unblocks=None, owner=None, agent=False,
               server_items=False) -> str:
    """The brief; *unblocks* is the server's full-graph map (the ``items`` of a
    JSON ``ready`` reply are only the ready subset, so recounting there is 0).

    *server_items*: ``items`` is a ``ready`` reply, already filtered, ordered and
    capped by the keeper; print it as-is.  Re-filtering would re-check
    dependencies against the reply subset and drop items whose done dependency
    is not in it (PG-CLI-READY-FIX).  The offline snapshot path keeps the filter."""
    if server_items:
        return views.brief_rows(items, unblocks or {})
    return views.brief(items, project=project, n=n, ub=unblocks, owner=owner, agent=agent)


def show_text(info: dict) -> str:
    """One compact block for one item: fields, deps, unblocks, evidence."""
    deps = ", ".join(f"{d['key']}={d.get('state')}"
                     for d in info.get("depends_on") or []) or "-"
    unblocks = ", ".join(info.get("unblocks") or []) or "-"
    lines = [
        f"{info['key']} | {info.get('title', '')}",
        f"state {info.get('state')} | kind {info.get('kind')} | "
        f"track {info.get('track')} | prio {info.get('priority')} | "
        f"epoch {info.get('epoch')}",
        f"claimed_by {info.get('claimed_by') or '-'} | "
        f"delegate {info.get('delegate') or '-'} | "
        f"lease {info.get('lease_until') or '-'} | "
        f"reviewer {info.get('reviewer') or '-'}",
        f"deps {deps}",
        f"unblocks {unblocks}",
    ]
    evidence = info.get("evidence") or []
    if evidence:
        lines.append("evidence " + "; ".join(
            f"{e['kind']}{'✓' if e.get('verified') else '✗'}" for e in evidence))
    else:
        lines.append("evidence -")
    if info.get("host"):
        lines.append(f"host {info['host']}")
    if info.get("wait"):
        lines.append(f"wait {info['wait']}")
    if info.get("fixes") or info.get("fixed_by"):
        lines.append(f"fixes {info.get('fixes') or '-'} fixed_by "
                     f"{','.join(info.get('fixed_by') or []) or '-'}")
    model = " ".join(f"{n} {info[n]}" for n in ("layer", "role", "agent_id", "phase_kind")
                     if info.get(n))
    if model:
        lines.append(model)
    if info.get("writer_model") or info.get("seat_models"):
        seats = ",".join(
            f"{s.get('model')}:{s.get('verdict') if s.get('verdict') is not None else '-'}"
            for s in info.get("seat_models") or []) or "-"
        lines.append(f"models writer={info.get('writer_model') or '-'} seats={seats}")
    if info.get("description"):
        lines.append(f"desc {info['description']}")
    if info.get("done_criteria"):
        lines.append(f"done {info['done_criteria']}")
    reviews = ", ".join(f"{r.get('reviewer')}({r.get('family') or '-'})"
                        for r in info.get("reviews") or []) or "-"
    if info.get("review_waiver"):
        reviews += f" | waiver {info['review_waiver']}"
    lines.append(f"reviews {reviews}")
    return "\n".join(lines)


def blocked_text(blocked: dict) -> str:
    if not blocked:
        return "no blocked items"
    return "\n".join(f"{key} <- {', '.join(deps)}"
                     for key, deps in sorted(blocked.items()))


def alerts_text(alerts: dict) -> str:
    lines = []
    for row in alerts.get("lease_expiring") or []:
        lines.append(f"lease {row['key']} by {row.get('claimed_by')} "
                     f"until {row.get('lease_until')}")
    for key in alerts.get("review_overdue") or []:
        lines.append(f"review_overdue {key}")
    for row in alerts.get("findings") or []:
        lines.append(f"finding {row.get('code')} {row.get('key') or '-'} "
                     f"{row.get('msg') or ''}".rstrip())
    for row in alerts.get("refusals") or []:
        lines.append(f"refusals {row['key']} x{row['count']}")
    for row in alerts.get("warnings") or []:
        lines.append(f"warning {row['code']} {row['key']}")
    if alerts.get("stalled"):
        lines.append("stalled true")
    return "\n".join(lines) if lines else "no alerts"


def aliases_text(rows) -> str:
    return "\n".join(f"{r['old']} -> {r['new']}" for r in rows or []) \
        or "no aliases"


def needs_you_text(rows) -> str:
    return "\n".join(
        f"{r['key']} | {r['title']} | {r['kind']} {r.get('wait') or '-'} | "
        f"unblocks {r['unblocks']} | prio {r['priority']}"
        for r in rows or []) or "nothing needs you"


def answers_text(rows) -> str:
    return "\n".join(
        f"{r['key']} | {r['decision_id']} | {r['done_at']} | {r['answer']}"
        + (f" | superseded_by {','.join(r['superseded_by'])}"
           if r.get("superseded_by") else "")
        for r in rows or []) or "no answers"


def waves_text(waves) -> str:
    return "\n".join(f"wave {i}: {', '.join(layer)}"
                     for i, layer in enumerate(waves or []))


def critical_text(path: dict) -> str:
    keys = path.get("keys") or []
    return (f"critical {path.get('hours', 0):g}h "
            f"{' -> '.join(keys) if keys else '-'}")


def percent_text(value) -> str:
    return f"{float(value or 0):.1f}%"


def digest_text(digest: dict) -> str:
    lines = [f"{name}: " + " ".join(
        f"{state}={counts.get(state, 0)}"
        for state in ("open", "claimed", "in_review", "done", "cancelled"))
        + (" stalled" if counts.get("stalled") else "")
        for name, counts in sorted((digest.get("projects") or {}).items())]
    for done in digest.get("done") or []:
        kinds = ",".join(done.get("evidence_kinds") or [])
        lines.append(f"done {done['key']} [{kinds}]")
    for waiver in digest.get("waivers") or []:
        state = f" ({waiver['state']})" if waiver.get("state") else ""
        lines.append(
            f"waiver {waiver['key']} {waiver.get('decision_id')}{state}")
    return "\n".join(lines) if lines else "empty"


def whoami_text(who: dict) -> str:
    projects = ",".join(who.get("projects") or []) or "-"
    return (f"{who.get('name')} | role {who.get('role')} | "
            f"projects {projects} | family {who.get('family') or '-'}")


def snapshot_text(snap: dict) -> str:
    items = snap.get("items") or []
    return (f"snapshot {len(items)} items | generated {snap.get('generated')} | "
            f"head {(snap.get('head') or '-')[:12]}")


def result_text(verb: str, result) -> str:
    """The smallest useful line for a write verb's result."""
    if isinstance(result, dict):
        if "created" in result:
            text = f"created {len(result['created'])} items"
            for warning in result.get("warnings") or []:
                text += f" | warning: {warning}"
            return text
        if verb == "claim":
            return (f"{result.get('key')} claimed epoch {result.get('epoch')} "
                    f"lease {result.get('lease_until')}")
        if verb == "verdict":
            if result.get("state") == "done":
                return "done (2/2)"
            if result.get("state") == "in_review":
                passed = int(result.get("reviews_passed") or 0)
                return (f"{passed}/2 passed — waiting for a second reviewer "
                        "of another family")
            return f"{result.get('key')} {result.get('state')}"
        if verb == "whoami":
            return whoami_text(result)
        if "state" in result:
            return f"{result.get('key')} {result.get('state')}"
        if "key" in result:
            return f"{result['key']} ok"
        if "expired" in result:
            return (f"reaped {len(result.get('expired') or [])} expired, "
                    f"{len(result.get('overdue') or [])} overdue")
    return str(result)


def progress_text(data) -> str:
    """PROGRESS-PANEL text: one block per project, one line per phase."""
    lines = []
    for p in (data or {}).get("projects") or []:
        st = p.get("states") or {}
        lines.append(
            f"{p['project']} | {p['pct_count']}% ({p['done']}/{p['total']}) | "
            f"{p['pct_hours']}% h, {p['hours_left']} h left | "
            f"open {st.get('open', 0)} claimed {st.get('claimed', 0)} "
            f"in_review {st.get('in_review', 0)} | operator {p['operator_open']} | "
            f"updated {p.get('last_update') or '-'}")
        for ph in p.get("phases") or []:
            lines.append(f"  {ph['phase']} | {ph['pct_count']}% ({ph['done']}/{ph['total']})"
                         f" | {ph['hours_left']} h left")
        for r in p.get("running") or []:
            lines.append(f"  running {r['key']} | {r.get('owner') or '-'} | {r['state']}"
                         f" | model {r.get('model') or 'unknown'}")
    total = (data or {}).get("total")
    if total:
        lines.append(f"total | {total['pct_count']}% ({total['done']}/{total['total']}) | "
                     f"{total['hours_left']} h left")
    return "\n".join(lines) or "no items"
