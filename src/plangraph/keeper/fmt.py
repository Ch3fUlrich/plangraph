"""Compact text renderers for keeper results.

The API's ``?fmt=text`` responses and the CLI's default output both come from
here, so an agent sees exactly the same bytes whichever door it used.  ``json``
is always the fallback; every helper only summarises what the store returned.
"""
from __future__ import annotations

from .. import views

__all__ = [
    "ready_text", "show_text", "blocked_text", "alerts_text", "waves_text",
    "critical_text", "percent_text", "digest_text", "whoami_text",
    "snapshot_text", "result_text",
]


def ready_text(items, project=None, n=10) -> str:
    return views.brief(items, project=project, n=n)


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
    if info.get("done_criteria"):
        lines.append(f"done {info['done_criteria']}")
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
    if alerts.get("stalled"):
        lines.append("stalled true")
    return "\n".join(lines) if lines else "no alerts"


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
            return f"created {len(result['created'])} items"
        if verb == "claim":
            return (f"{result.get('key')} claimed epoch {result.get('epoch')} "
                    f"lease {result.get('lease_until')}")
        if verb == "verdict":
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
