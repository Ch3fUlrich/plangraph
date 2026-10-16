"""HTTP doors for the question/answer inbox, thin over the existing store verbs.

``GET /v1/questions`` lists questions with their answer; ``POST /v1/ask_question``
and ``POST /v1/answer_question`` call ``Store.ask_question`` /
``Store.answer_question`` unchanged; ``POST /v1/resignal_question`` re-sends the
Temporal signal for an already answered (or timed-out) question.  An answer sends
its signal only after the store commit, and a signal failure never fails or rolls
back the answer.  The keeper runs no deadline timer: that lives in Temporal.

``GET /v1/alarms`` lists the open (uncleared, unacknowledged) projector alarms and
``POST /v1/ack_alarm`` acknowledges one for good (router or system only).  The
acknowledgement *is* the audit row: the successful ``ack_alarm`` ``audit_log`` entry
keyed ``alarm:<id>`` that ``Store._run`` writes on success, so there is no separate
acks table and no schema change; a refusal is audited ``ok=0`` and hides nothing.

All handlers run after bearer auth and ``api_routes.run_gate`` in ``api.py``.
"""
from __future__ import annotations

import json

from ..keeper import db_errors
from ..keeper.errors import Refused
from . import temporal_signal

__all__ = ["READ_VERBS", "WRITE_VERBS", "PAGE_HEADERS", "page", "read", "write"]

READ_VERBS = {"questions", "alarms"}
WRITE_VERBS = {"ask_question", "answer_question", "resignal_question", "ack_alarm"}

_STATES = ("open", "answered", "timeout", "all")
#: Roles that see every project's questions; all others see their own projects.
_SEES_ALL = ("router", "system", "reader")
_SELECT = ("SELECT q.id, q.key, q.text, q.default_answer, q.deadline, q.risk_tags,"
           " q.state, q.created_at, a.ts AS a_ts, a.actor AS a_actor,"
           " a.text AS a_text, a.source AS a_source"
           " FROM questions q LEFT JOIN answers a ON a.question_id = q.id")
#: Open projector alarms nobody has acknowledged yet: no ``ack_alarm`` audit row
#: (``ok=1``) for ``alarm:<id>`` (portable SQL, sqlite and postgres alike).
_ALARMS = ("SELECT id, key, kind, workflow_id, detail, raised_at FROM projector_alarms"
           " WHERE cleared_at IS NULL"
           " AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.action = 'ack_alarm'"
           " AND a.ok = 1 AND a.key = 'alarm:' || CAST(projector_alarms.id AS TEXT))"
           " ORDER BY id")
#: Every database failure an alarms read can take (a missing table included): the
#: caller gets a named error instead of a silently empty list.
_UNREADABLE = db_errors.NO_TABLE + db_errors.OPERATIONAL + db_errors.PROGRAMMING + db_errors.DENIED


#: Headers for ``GET /inbox``: a static page that may only call its own origin.
PAGE_HEADERS = (
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline';"
     " script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"),
    ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"))


def page() -> bytes:
    """The ``GET /inbox`` body: ``inbox.form.PAGE``, or a stub if it is absent."""
    try:
        from .form import PAGE
    except ImportError:
        PAGE = "<!doctype html><title>inbox</title>"
    return PAGE.encode("utf-8")


def _one(query: dict, name: str, default=None):
    values = query.get(name)
    return values[0] if values else default


def _need(body: dict, name: str):
    if body.get(name) is None:
        raise Refused("validation", f"missing field {name!r}")
    return body[name]


def _tags(raw) -> list:
    """``risk_tags`` is stored as JSON text; always return a list."""
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _project(key: str) -> str:
    return key.split(":")[0]


def _question(row) -> dict:
    """One API question dict from a joined questions/answers row."""
    answer = None
    if row["a_ts"] is not None:
        answer = {"ts": row["a_ts"], "actor": row["a_actor"],
                  "text": row["a_text"], "source": row["a_source"]}
    return {"id": row["id"], "key": row["key"], "project": _project(row["key"]),
            "text": row["text"], "default_answer": row["default_answer"],
            "deadline": row["deadline"], "risk_tags": _tags(row["risk_tags"]),
            "state": row["state"], "created_at": row["created_at"], "answer": answer}


def _list(store, principal, query: dict) -> dict:
    """Questions filtered by state/key/project and by what *principal* may see."""
    state = _one(query, "state", "open")
    if state not in _STATES:
        raise Refused("validation", "state must be open, answered, timeout or all")
    key, project = _one(query, "key"), _one(query, "project")

    def fn(now):
        clauses, params = [], []
        if state != "all":
            clauses.append("q.state = ?")
            params.append(state)
        if key:
            clauses.append("q.key = ?")
            params.append(store.resolve_key(key))
        sql = _SELECT + (" WHERE " + " AND ".join(clauses) if clauses else "")
        rows = store.conn.execute(sql + " ORDER BY q.id", tuple(params)).fetchall()
        allowed = None if principal.role in _SEES_ALL else set(principal.projects or [])
        return [_question(r) for r in rows
                if (project is None or _project(r["key"]) == project)
                and (allowed is None or _project(r["key"]) in allowed)]

    return {"questions": store._run(principal, "questions", None, fn)}


def _age_s(store, stamp, moment) -> int | None:
    """Whole seconds between the ISO *stamp* and *moment*; ``None`` if unparsable."""
    when = store._parse_iso(stamp)
    return None if when is None else int(moment - when.timestamp())


def _alarms(store, principal) -> dict:
    """Every open, unacknowledged projector alarm, oldest first."""
    def fn(now):
        try:
            rows = store.conn.execute(_ALARMS).fetchall()
        except _UNREADABLE as exc:      # table missing or unreadable: named, never silent
            return {"alarms": [], "error": f"alarms unreadable: {type(exc).__name__}"}
        moment = store.now()
        return {"alarms": [{"id": r["id"], "key": r["key"], "kind": r["kind"],
                            "workflow_id": r["workflow_id"], "detail": r["detail"],
                            "raised_at": r["raised_at"],
                            "age_s": _age_s(store, r["raised_at"], moment)}
                           for r in rows]}

    return store._run(principal, "alarms", None, fn)


def _stored_answer(store, question_id) -> dict | None:
    """The stored answer of *question_id* as a signal request, or ``None``."""
    row = store.conn.execute(
        "SELECT q.id, q.key, q.state, a.text, a.source, a.ts FROM questions q"
        " LEFT JOIN answers a ON a.question_id = q.id WHERE q.id = ?",
        (question_id,)).fetchone()
    if row is None:
        return None
    return {"question_id": row["id"], "key": row["key"], "state": row["state"],
            "text": row["text"], "source": row["source"], "answered_at": row["ts"]}


def _signal(store, question_id) -> dict:
    """Send the signal for a committed answer; any failure is a status, not a raise."""
    try:
        question = _stored_answer(store, question_id)
        if question is None or question["source"] is None:
            return {"status": "failed", "workflow_id": None}
        return temporal_signal.send_answer(question, store.conn)
    except Exception:  # noqa: BLE001 - the answer is already committed
        return {"status": "failed", "workflow_id": None}


def _ask(store, principal, body: dict) -> dict:
    return store.ask_question(principal, _need(body, "key"), body.get("text"),
                              body.get("default_answer"), body.get("deadline"),
                              body.get("risk_tags") or ())


def _answer(store, principal, body: dict) -> dict:
    result = store.answer_question(principal, body.get("question_id"),
                                   body.get("text"), body.get("source", "answer"))
    return dict(result, signal=_signal(store, result["question_id"]))


def _resignal(store, principal, body: dict) -> dict:
    qid = body.get("question_id")
    row = None
    if isinstance(qid, int) and not isinstance(qid, bool):
        row = store.conn.execute("SELECT key FROM questions WHERE id=?",
                                 (qid,)).fetchone()

    def fn(now):
        if principal.role not in ("router", "system"):
            raise Refused("forbidden", "resignal_question is router or system only")
        if isinstance(qid, bool) or not isinstance(qid, int):
            raise Refused("validation", "question_id must be an integer")
        question = _stored_answer(store, qid)
        if question is None:
            raise Refused("not_found", f"no question {qid}")
        if question["state"] not in ("answered", "timeout"):
            raise Refused("bad_state", f"question {qid} is {question['state']}")
        return {"question_id": qid, "signal": _signal(store, qid)}

    return store._run(principal, "resignal_question", row["key"] if row else None, fn)


def _ack(store, principal, body: dict) -> dict:
    aid = body.get("alarm_id")
    key = (f"alarm:{aid}" if isinstance(aid, int) and not isinstance(aid, bool)
           else None)                       # the audit key, before _run (like _resignal)

    def fn(now):
        if principal.role not in ("router", "system"):
            raise Refused("forbidden", "ack_alarm is router or system only")
        if isinstance(aid, bool) or not isinstance(aid, int):
            raise Refused("validation", "alarm_id must be an integer")
        row = store.conn.execute("SELECT cleared_at FROM projector_alarms WHERE id=?",
                                 (aid,)).fetchone()
        if row is None:
            raise Refused("not_found", f"no alarm {aid}")
        if row["cleared_at"] is not None:
            raise Refused("bad_state", f"alarm {aid} is cleared")
        # the ack is the ``ok=1`` audit row ``_run`` writes for this key on success;
        # a repeat ack just adds another one and stays idempotent.
        return {"alarm_id": aid, "acked": True}

    return store._run(principal, "ack_alarm", key, fn)


def read(store, principal, name: str, query: dict) -> dict:
    """Dispatch a GET inbox verb (``query`` is the ``parse_qs`` mapping)."""
    if name == "questions":
        return _list(store, principal, query)
    if name == "alarms":
        return _alarms(store, principal)
    raise Refused("not_found", f"no read verb {name!r}")


def write(store, principal, name: str, body: dict) -> dict:
    """Dispatch a POST inbox verb."""
    if name == "ask_question":
        return _ask(store, principal, body)
    if name == "answer_question":
        return _answer(store, principal, body)
    if name == "resignal_question":
        return _resignal(store, principal, body)
    if name == "ack_alarm":
        return _ack(store, principal, body)
    raise Refused("not_found", f"no write verb {name!r}")
