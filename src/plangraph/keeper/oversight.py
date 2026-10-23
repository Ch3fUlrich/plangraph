"""RT-OVERSIGHT-CHECKS: pure checks R1..R7 over a keeper snapshot.

Every rule is a pure function of ``conn`` and an injected epoch ``now``: no wall
clock, no network, no LLM.  A *finding* is a plain dict ``{rule, kind, key,
reason, detail, ts}``; ``reason`` is the stable dedupe key (``rule:kind:key``,
optionally with the underlying alarm kind for R6) and carries no timestamps or
age numbers.  ``stall_no_event``/``merged_open`` come from
:func:`plangraph.keeper.stall.stall_alarms` so there is one stall implementation
in the tree.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone

from .. import views_graph
from ..governor.policy import BURN_LIMIT
from .stall import STALL_S, _CLOSED, stall_alarms
from .store_support import iso, parse_iso

__all__ = [
    "STALL_S",
    "LANE_IDLE_S",
    "QUESTION_GRACE_S",
    "INBOX_WINDOW_S",
    "BURN_LIMIT",
    "PROJECTOR_STALE_S",
    "r1_r2",
    "r3_critical_idle",
    "r4_lane_idle",
    "r5_windows",
    "r6_projector",
    "r7_burn",
    "run_checks",
    "digest",
    "raise_alarms",
]

#: A lane branch whose last commit (and item activity) is older than a day is idle.
LANE_IDLE_S = 24 * 3600.0
#: Alarm as soon as a question deadline is past (``now > deadline``).
QUESTION_GRACE_S = 0
#: An operator item waiting in the inbox longer than three days is overdue.
INBOX_WINDOW_S = 72 * 3600.0
#: The projector's global watermark may lag this long before it is stale.
PROJECTOR_STALE_S = 1800.0

_LANE_REF_PREFIX = "refs/remotes/origin/lane/"
_OPERATOR_KINDS = ("question", "decision", "operator_task")
_ITEM_COLS = ("key, project, kind, state, operator, priority,"
              " updated_at, created_at")


# ------------------------------------------------------------------ helpers
def _age(stamp, now):
    """Whole seconds from ISO *stamp* to epoch *now*; ``None`` if unparsable."""
    if not stamp:
        return None
    try:
        when = parse_iso(stamp)
    except Exception:  # noqa: BLE001 - a malformed stamp is simply unknown
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(float(now) - when.timestamp())


def _finding(rule, kind, key, detail, ts, reason=None):
    key = key or ""                     # a NULL column never reaches _sort/JSON
    return {"rule": rule, "kind": kind, "key": key,
            "reason": reason or f"{rule}:{kind}:{key}",
            "detail": str(detail or ""), "ts": ts or ""}


def _sort(rows):
    return sorted(rows, key=lambda f: (f["rule"], f["key"], f["kind"],
                                       f["reason"], f["detail"], f["ts"]))


def _items(conn):
    """Every item as a small dict, plus ``deps`` from the (src, dst) edges."""
    deps = {}
    for row in conn.execute("SELECT src, dst FROM edges WHERE rel='depends_on'"):
        deps.setdefault(row["src"], []).append(row["dst"])
    out = []
    for row in conn.execute(f"SELECT {_ITEM_COLS} FROM items ORDER BY key"):
        item = dict(row)
        item["deps"] = sorted(deps.get(item["key"], []))
        out.append(item)
    return out


def _last_events(conn):
    """``{key: newest events.ts}`` for every key that has events."""
    return {row["key"]: row["last_ts"] for row in conn.execute(
        "SELECT key, MAX(ts) AS last_ts FROM events"
        " WHERE key IS NOT NULL GROUP BY key")}


def _slug(value):
    """Lowercase, non-alphanumeric runs collapsed to ``-`` (``demo:A`` -> ``demo-a``)."""
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


def _table_exists(conn, name):
    try:
        if getattr(conn, "backend", "sqlite") == "postgres":
            row = conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name=?"
                " AND table_schema=current_schema()"
                " LIMIT 1", (name,)).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,)).fetchone()
    except Exception:  # noqa: BLE001 - a missing catalogue is "no table"
        return False
    return row is not None


# ------------------------------------------------------------------ R1, R2
_RULE_OF_STALL = {"stall_no_event": "R1", "merged_open": "R2"}
#: Alarm kind handed back to the rule that owns it.  When a rule's check fails
#: (``oversight_check_failed``) :func:`raise_alarms` must keep its open alarms
#: instead of reading the missing findings as "the condition went away".
_RULE_OF_ALARM_KIND = {"lane_idle": "R4"}


def r1_r2(conn, now, git=None):
    """Map :func:`stall.stall_alarms` rows onto R1 and R2 findings.

    Only data-derived stamps reach the finding: R1's ``detail`` and ``ts`` come
    from the stall row's ``raised_at`` (never the stall age, which grows with
    ``now``), and R2's ``ts`` stays empty because ``merged_open`` stamps
    ``raised_at`` with ``iso(now)``.  That keeps :func:`digest` stable across
    two runs over one snapshot.
    """
    rows = []
    for alarm in stall_alarms(conn, now, git=git):
        rule = _RULE_OF_STALL.get(alarm["kind"])
        if rule is None:
            continue
        raised_at = alarm.get("raised_at") or ""
        if rule == "R1":
            detail, ts = f"claimed, no event since {raised_at}", raised_at
        else:
            detail, ts = alarm["detail"], ""
        rows.append(_finding(rule, alarm["kind"], alarm["key"], detail, ts))
    return _sort(rows)


# --------------------------------------------------------------- R3
def r3_critical_idle(conn, now):
    """Open, unclaimed, dependency-ready items idling on an open gate path."""
    items = _items(conn)
    state = {it["key"]: it["state"] for it in items}
    preds = {it["key"]: set(it["deps"]) for it in items}
    gates = [it["key"] for it in items
             if it["kind"] in ("gate", "goal") and it["state"] not in _CLOSED]
    on_gate_path = views_graph.ancestors(gates, preds) if gates else set()
    last_events = _last_events(conn)
    rows = []
    for item in items:
        key = item["key"]
        if key not in on_gate_path or item["state"] in _CLOSED:
            continue
        if item["state"] == "claimed":          # claimed work belongs to R1
            continue
        if any(state.get(dep) != "done" for dep in item["deps"]):
            continue
        stamp = last_events.get(key) or item["updated_at"] or item["created_at"]
        age = _age(stamp, now)
        if age is None or age <= STALL_S:
            continue
        detail = (f"{item['state']} {key} idle with all deps done"
                  f" (last {stamp})")
        rows.append(_finding("R3", "critical_idle", key, detail, stamp))
    return _sort(rows)


# --------------------------------------------------------------- R4
def _short_ref(refname):
    """``refs/remotes/origin/x`` -> ``origin/x`` (a rev git can resolve)."""
    return refname[len("refs/remotes/"):] if refname.startswith("refs/remotes/") \
        else refname


def r4_lane_idle(conn, now, git=None):
    """Lane branches ahead of main whose commit and item have both gone quiet.

    *git* is the same injectable shape the check uses for stall trailers but
    here as ``git(args) -> str``; without it the rule is skipped silently, so
    a caller must then pass ``keep_rules=("R4",)`` to :func:`raise_alarms`.
    A *git* that raises while listing means the check itself could not run, so
    it fails closed with one ``oversight_check_failed`` finding (never ``[]``:
    an empty result would let :func:`raise_alarms` clear open ``lane_idle``
    alarms).  Per-ref ``rev-list`` failures still only skip that one ref.
    """
    if git is None:
        return []
    try:
        listing = git(["for-each-ref",
                       "--format=%(refname) %(committerdate:unix)",
                       _LANE_REF_PREFIX])
    except Exception as exc:  # noqa: BLE001 - fail closed, keep R4's alarms open
        return [_finding("R4", "oversight_check_failed", "r4",
                         type(exc).__name__, "")]
    slugs = {}
    for item in _items(conn):
        slugs.setdefault(_slug(item["key"]), item["key"])
    last_events = _last_events(conn)
    found = {}
    for line in str(listing or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith(_LANE_REF_PREFIX):
            continue
        refname, stamp = parts[0], parts[1]
        try:
            commit_ts = float(stamp)
        except ValueError:
            continue
        if float(now) - commit_ts <= LANE_IDLE_S:
            continue                                # the branch moved recently
        name = refname[len(_LANE_REF_PREFIX):]
        try:
            out = git(["rev-list", "--count",
                       "origin/main.." + _short_ref(refname)])
            ahead = int(str(out).strip().splitlines()[0])
        except Exception:  # noqa: BLE001 - unknown divergence means skip
            continue
        if ahead <= 0:
            continue
        key = slugs.get(_slug(name)) or f"lane:{name}"
        age = _age(last_events.get(key), now)
        if age is not None and age <= LANE_IDLE_S:
            continue                                # the item is still busy
        detail = (f"lane {name} is {ahead} commit(s) ahead of main, last"
                  f" commit {iso(commit_ts)}")
        found[key] = _finding("R4", "lane_idle", key, detail, iso(commit_ts))
    return _sort(found.values())


# --------------------------------------------------------------- R5
def r5_windows(conn, now):
    """Overdue questions and operator items ageing in the inbox."""
    rows = []
    # Same silent skip as r6/r7: a snapshot without the questions table simply
    # has no question findings.
    if _table_exists(conn, "questions"):
        for row in conn.execute(
                "SELECT key, deadline FROM questions"
                " WHERE state='open' AND deadline IS NOT NULL ORDER BY key"):
            age = _age(row["deadline"], now)
            if age is None or age <= QUESTION_GRACE_S:
                continue
            detail = (f"question {row['key']} is open past its deadline"
                      f" {row['deadline']}")
            rows.append(_finding("R5", "question_overdue", row["key"], detail,
                                 row["deadline"]))
    for item in _items(conn):
        if item["state"] in _CLOSED:
            continue
        if not (item["operator"] or item["kind"] in _OPERATOR_KINDS):
            continue
        stamp = item["updated_at"] or item["created_at"]
        age = _age(stamp, now)
        if age is None or age <= INBOX_WINDOW_S:
            continue
        detail = (f"operator item {item['key']} ({item['kind']}) waiting"
                  f" in the inbox (since {stamp})")
        rows.append(_finding("R5", "inbox_overdue", item["key"], detail, stamp))
    return _sort(rows)


# --------------------------------------------------------------- R6
def r6_projector(conn, now):
    """A stale global watermark plus every open projector alarm."""
    if not _table_exists(conn, "projector_watermark"):
        return []
    rows = []
    row = conn.execute(
        "SELECT updated_at FROM projector_watermark WHERE workflow_id='*'"
    ).fetchone()
    if row is None:
        rows.append(_finding("R6", "watermark_stale", "global",
                             "no global projector watermark", ""))
    else:
        age = _age(row["updated_at"], now)
        if age is None or age > PROJECTOR_STALE_S:
            rows.append(_finding(
                "R6", "watermark_stale", "global",
                f"global watermark is stale (limit {int(PROJECTOR_STALE_S)}s;"
                f" last {row['updated_at'] or 'unknown'})",
                row["updated_at"] or ""))
    if _table_exists(conn, "projector_alarms"):
        # Oversight's own rows (workflow_id='oversight') are outputs of this
        # module, not projector conditions: reporting them would feed back.
        for alarm in conn.execute(
                "SELECT kind, key, detail, raised_at FROM projector_alarms"
                " WHERE cleared_at IS NULL AND (workflow_id IS NULL"
                " OR workflow_id <> 'oversight')"
                " ORDER BY id"):
            reason = f"R6:reconcile_alarm:{alarm['kind']}:{alarm['key'] or ''}"
            # The finding key carries the alarm kind so two open alarms that
            # share a key but differ in kind stay distinct in raise_alarms.
            rows.append(_finding("R6", "reconcile_alarm",
                                 f"{alarm['kind']}:{alarm['key'] or ''}",
                                 alarm["detail"], alarm["raised_at"],
                                 reason=reason))
    return _sort(rows)


# --------------------------------------------------------------- R7
def r7_burn(conn, day, now):
    """The daily burn index above :data:`BURN_LIMIT`."""
    if not _table_exists(conn, "metrics"):
        return []
    row = conn.execute(
        "SELECT value FROM metrics WHERE day=? AND metric='claude_burn_index'",
        (day,)).fetchone()
    if row is None:
        return []
    try:
        value = float(row["value"])
    except (TypeError, ValueError):
        return []
    if not math.isfinite(value) or value <= BURN_LIMIT:
        return []
    detail = f"burn index {value} on {day} exceeds limit {BURN_LIMIT}"
    return [_finding("R7", "burn_over", str(day), detail, str(day))]


# ------------------------------------------------------------ aggregate
def run_checks(conn, now, *, git=None, lane_git=None, day=None):
    """Every rule, one list sorted by ``(rule, key)``.

    ``git`` is the config-style object (or ``(repo, ref)`` callable) that
    :mod:`plangraph.keeper.stall` understands for R1/R2; ``lane_git`` is the
    separate ``git(args) -> str`` callable R4 needs.  They are deliberately not
    interchangeable: a config object silently yields no R4 rows.
    """
    if day is None:
        day = datetime.fromtimestamp(float(now), tz=timezone.utc).date().isoformat()
    findings = []
    findings.extend(r1_r2(conn, now, git=git))
    findings.extend(r3_critical_idle(conn, now))
    findings.extend(r4_lane_idle(conn, now, git=lane_git))
    findings.extend(r5_windows(conn, now))
    findings.extend(r6_projector(conn, now))
    findings.extend(r7_burn(conn, day, now))
    return _sort(findings)


def digest(findings):
    """``(text, sha256hex)``: the canonical JSON payload and its digest."""
    text = json.dumps(_sort(findings),
                      sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True) + "\n"
    return text, hashlib.sha256(text.encode("ascii")).hexdigest()


# --------------------------------------------------------------- alarms
def raise_alarms(conn, findings, now, keep_rules=()):
    """Open one ``projector_alarms`` row per new finding; clear vanished ones.

    Rows live under ``workflow_id='oversight'`` and dedupe on ``(kind, key)``
    while open (a finding's ``reason`` is carried in ``detail``).  New ids are
    ``MAX(id)+1`` inside one write transaction, exactly like the reconciler.
    An already-open row whose finding detail changed has only its ``detail``
    updated: the row keeps its ``id``, its ``raised_at`` (the alarm's age) and
    stays a single alarm.  An open row whose kind belongs to a rule that
    reported ``oversight_check_failed`` is *kept*: the rule's findings are
    missing because the check could not run, not because the condition
    vanished.  ``keep_rules`` names further rules that did not run at all this
    pass (for instance R4 without a lane git adapter), so their open alarms are
    kept too.  Returns the number of *new* rows (refreshes are not new rows).
    """
    stamp = iso(now)
    started = not getattr(conn, "in_transaction", False)
    if started:
        conn.execute("BEGIN IMMEDIATE")
    try:
        failed = {f["rule"] for f in findings
                  if f["kind"] == "oversight_check_failed"}
        kept = failed | set(keep_rules)
        present = {}
        for row in conn.execute(
                "SELECT id, kind, key, detail FROM projector_alarms"
                " WHERE cleared_at IS NULL AND workflow_id='oversight'"
                " ORDER BY id"):
            present[(row["kind"], row["key"])] = (row["id"], row["detail"])
        wanted = {}
        for finding in findings:
            wanted.setdefault((finding["kind"], finding["key"]), finding)
        new = 0
        for ident, finding in wanted.items():
            detail = f"{finding['reason']}: {finding.get('detail', '')}".strip()
            if ident in present:
                alarm_id, current = present[ident]
                # Same open alarm, possibly a newer explanation: refresh the
                # text in place, never the raise stamp and never a second row.
                if current != detail:
                    conn.execute(
                        "UPDATE projector_alarms SET detail=? WHERE id=?",
                        (detail, alarm_id))
                continue
            next_id = conn.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM projector_alarms"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO projector_alarms"
                "(id, kind, workflow_id, key, detail, raised_at)"
                " VALUES(?,?,?,?,?,?)",
                (next_id, ident[0], "oversight", ident[1], detail,
                 finding.get("ts") or stamp))
            new += 1
        for ident, (alarm_id, _detail) in present.items():
            if ident not in wanted:
                owner = _RULE_OF_ALARM_KIND.get(ident[0])
                if owner is not None and owner in kept:
                    continue        # the owning rule could not run: keep it
                conn.execute(
                    "UPDATE projector_alarms SET cleared_at=? WHERE id=?",
                    (stamp, alarm_id))
        if started:
            conn.commit()
    except BaseException:
        if started:
            conn.rollback()
        raise
    return new
