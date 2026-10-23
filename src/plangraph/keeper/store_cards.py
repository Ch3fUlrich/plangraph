"""Design-3 (W2) store verbs: cards, attempts, decisions, questions/answers, budget, states.

Every verb is portable SQL (``?`` placeholders, ``ON CONFLICT`` upserts, ids assigned
as ``MAX(id)+1`` inside the write lock) so it runs unchanged on SQLite and Postgres.
Only ``set_state`` touches the hash chain, with the new ``state_set`` event; the other
tables are plain rows (``attempts``, ``decisions`` and ``answers`` are append-only by
trigger).

* ``put_card`` / ``get_card``: the 1:1 card of an item (full replace).
* ``record_attempt``: one finished attempt (append-only; ``exit_class`` is required).
* ``add_decision``: an operator decision or judge ruling (router only).
* ``ask_question`` / ``answer_question``: a structured question with an optional default
  and deadline.  A risk-tagged question never has a default.  ``source='question_timeout'``
  applies the default once the deadline has passed.
* ``set_budget``: absolute tokens/cost/meter for one provider and day.
* ``set_state``: move an item among the non-claim states (open, ready, running,
  needs_input, waiting, failed, parked); claim, review, done and cancel keep their verbs.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date

from plangraph.card_paths import normalise_path, path_problem

from .credential_guard import guard
from .errors import Refused
from .schema import EXIT_CLASSES, RISK_TAGS, WINDOW_CLASSES

__all__ = ["CardsMixin", "SETTABLE_STATES", "NO_DEFAULT_TAGS"]

#: States ``set_state`` moves between (none of them carries a claim, lease or epoch).
SETTABLE_STATES = ("open", "ready", "running", "needs_input", "waiting", "failed",
                   "parked")
#: Question tags: the card tags plus the design's "public release".
QUESTION_TAGS = RISK_TAGS + ("public-release",)
#: design 4.8: a question carrying one of these has no default and is never timed out.
NO_DEFAULT_TAGS = frozenset({"auth", "secrets", "money", "live-step", "public-release"})

_CARD_LISTS = ("read_set", "write_set", "exports", "forbid_paths", "smoke_checks", "check_reads")
_CARD_TEXT = ("check_cmd", "heldout_check")
_CARD_KEYS = frozenset(("outcome", "window_class", "risk_tags", "estimate_tokens",
                        "check_kind", *_CARD_LISTS, *_CARD_TEXT))
_ATTEMPT_KEYS = frozenset((
    "n", "harness", "model", "combo", "class", "started_at", "ended_at", "exit_class",
    "tokens_in", "tokens_out", "cost", "run_id", "workflow_id", "log_ref"))
_ATTEMPT_TEXT = ("harness", "model", "combo", "class", "run_id", "workflow_id", "log_ref")
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
_PROVIDER_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
_TEXT_CAP = 2000
_LIST_CAP = 500
_ITEM_CAP = 500


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_list(value):
    try:
        parsed = json.loads(value) if isinstance(value, str) else (value or [])
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _text(name, value, required=False, cap=_TEXT_CAP, key=None):
    if value is None:
        if required:
            raise Refused("validation", f"{name} is required")
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise Refused("validation", f"{name} must be a non-empty string"
                      if required else f"{name} must be a string or null")
    if len(value) > cap:
        raise Refused("validation", f"{name} exceeds {cap} characters")
    guard(name, value, key)
    return value


def _str_list(name, value, allowed=None, key=None):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > _LIST_CAP:
        raise Refused("validation", f"{name} must be a list of at most {_LIST_CAP} strings")
    out = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip() or len(entry) > _ITEM_CAP:
            raise Refused("validation", f"{name} entries must be non-empty strings"
                          f" of at most {_ITEM_CAP} characters")
        if allowed is not None and entry not in allowed:
            raise Refused("validation", f"{name} entry {entry!r} is not one of"
                          f" {list(allowed)}")
        guard(name, entry, key)
        if entry not in out:
            out.append(entry)
    return out


def _count(name, value, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Refused("validation", f"{name} must be a non-negative integer"
                      + (" or null" if nullable else ""))
    return value


def _number(name, value, nullable=True):
    if value is None and nullable:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise Refused("validation", f"{name} must be a non-negative number"
                      + (" or null" if nullable else ""))
    return float(value)


class CardsMixin:
    """The W2 verbs for :class:`plangraph.keeper.store.Store`."""

    # ------------------------------------------------------------- helpers
    def _may_write_for(self, principal, project):
        """router/system anywhere; an l1 only in its own project."""
        if principal.role in ("router", "system"):
            return
        self._may_manage(principal, project)

    def _may_act_in(self, principal, project):
        if principal.role in ("router", "system"):
            return
        self._require_role(principal, "l1", "worker", "spawner")
        self._require_project(principal, project)

    def _iso_or_none(self, name, value):
        if value is None:
            return None
        if not isinstance(value, str) or self._parse_iso(value) is None:
            raise Refused("validation", f"{name} must be an ISO timestamp or null")
        return value

    # ---------------------------------------------------------------- cards
    def put_card(self, principal, key, card):
        return self._run(principal, "put_card", key,
                         lambda now: self._put_card(principal, key, card, now))

    def _put_card(self, principal, key, card, now):
        self._require_role(principal, "router", "l1", "system")
        if not isinstance(card, dict):
            raise Refused("validation", "card must be an object")
        unknown = set(card) - _CARD_KEYS
        if unknown:
            raise Refused("validation", f"unknown card fields: {sorted(unknown)}")
        window = card.get("window_class")
        if window is not None and window not in WINDOW_CLASSES:
            raise Refused("validation", f"window_class must be one of {list(WINDOW_CLASSES)}")
        check_kind = card.get("check_kind", "change")
        if check_kind not in ("change", "regression"):
            raise Refused("validation", "check_kind must be change or regression")
        values = {
            "check_kind": check_kind,
            "outcome": _text("outcome", card.get("outcome"), required=True, key=key),
            "check_cmd": _text("check_cmd", card.get("check_cmd"), key=key),
            "heldout_check": _text("heldout_check", card.get("heldout_check"), key=key),
            "window_class": window,
            "risk_tags": _dump(_str_list("risk_tags", card.get("risk_tags"),
                                         RISK_TAGS, key=key)),
            "estimate_tokens": _count("estimate_tokens", card.get("estimate_tokens")),
        }
        for name in _CARD_LISTS:
            entries = _str_list(name, card.get(name), key=key)
            if name == "check_reads":
                normalised = []
                for entry in entries:
                    path = normalise_path(entry)
                    problem = path_problem(name, entry, path)
                    if problem:
                        raise Refused("validation", problem)
                    if path not in normalised:
                        normalised.append(path)
                entries = normalised
            values[name] = _dump(entries)
        with self._write():
            item = self._get_or_refuse(key)
            self._may_write_for(principal, item["project"])
            self._assert_unlocked(item)
            cols = ["outcome", *_CARD_LISTS, "check_kind", "check_cmd", "heldout_check",
                    "window_class", "risk_tags", "estimate_tokens"]
            marks = ",".join("?" for _ in cols)
            sets = ",".join(f"{c}=excluded.{c}" for c in cols)
            self.conn.execute(
                f"INSERT INTO cards(key,{','.join(cols)},updated_at)"
                f" VALUES(?,{marks},?)"
                f" ON CONFLICT(key) DO UPDATE SET {sets},updated_at=excluded.updated_at",
                (key, *[values[c] for c in cols], self._iso(now)))
        return {"key": key}

    def get_card(self, principal, key):
        return self._run(principal, "get_card", key, lambda now: self._get_card(key))

    def _get_card(self, key):
        row = self.conn.execute("SELECT * FROM cards WHERE key=?", (key,)).fetchone()
        if row is None:
            raise Refused("not_found", f"no card for {key!r}")
        card = dict(row)
        for name in (*_CARD_LISTS, "risk_tags"):
            card[name] = _load_list(card[name])
        return card

    # ------------------------------------------------------------- attempts
    def record_attempt(self, principal, key, attempt):
        result = self._run(principal, "record_attempt", key,
                           lambda now: self._record_attempt(principal, key, attempt))
        self._fill_jev_outcome(principal, result["key"],
                               {"exit_class": result["exit_class"]}, attempt=result["n"])
        return result

    def _record_attempt(self, principal, key, attempt):
        self._require_role(principal, "router", "l1", "system")
        if not isinstance(attempt, dict):
            raise Refused("validation", "attempt must be an object")
        unknown = set(attempt) - _ATTEMPT_KEYS
        if unknown:
            raise Refused("validation", f"unknown attempt fields: {sorted(unknown)}")
        exit_class = attempt.get("exit_class")
        if exit_class not in EXIT_CLASSES:
            raise Refused("validation", f"exit_class must be one of {list(EXIT_CLASSES)}")
        number = attempt.get("n")
        if number is not None and (isinstance(number, bool) or not isinstance(number, int)
                                   or number < 1):
            raise Refused("validation", "n must be a positive integer or null")
        started = self._iso_or_none("started_at", attempt.get("started_at"))
        ended = self._iso_or_none("ended_at", attempt.get("ended_at"))
        if started and ended and self._parse_iso(ended) < self._parse_iso(started):
            raise Refused("validation", "ended_at is before started_at")
        text = {name: _text(name, attempt.get(name), cap=512, key=key)
                for name in _ATTEMPT_TEXT}
        tokens_in = _count("tokens_in", attempt.get("tokens_in"))
        tokens_out = _count("tokens_out", attempt.get("tokens_out"))
        cost = _number("cost", attempt.get("cost"))
        with self._write():
            item = self._get_or_refuse(key)
            self._may_write_for(principal, item["project"])
            if number is None:
                number = self.conn.execute(
                    "SELECT COALESCE(MAX(n), 0) + 1 FROM attempts WHERE key=?",
                    (key,)).fetchone()[0]
            elif self.conn.execute("SELECT 1 FROM attempts WHERE key=? AND n=?",
                                   (key, number)).fetchone() is not None:
                raise Refused("conflict", f"{key} already has attempt {number}")
            row = self.conn.execute(
                "INSERT INTO attempts(id,key,n,harness,model,combo,class,started_at,"
                "ended_at,exit_class,tokens_in,tokens_out,cost,run_id,workflow_id,log_ref)"
                " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM attempts),"
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (key, number, text["harness"], text["model"], text["combo"],
                 text["class"], started, ended, exit_class, tokens_in, tokens_out,
                 cost, text["run_id"], text["workflow_id"], text["log_ref"])).fetchone()
        return {"key": key, "n": number, "id": row[0], "exit_class": exit_class}

    # ------------------------------------------------------------ decisions
    def add_decision(self, principal, decision_id, kind, text, closes=(), waives=()):
        return self._run(principal, "add_decision", None,
                         lambda now: self._add_decision(principal, decision_id, kind,
                                                        text, closes, waives, now))

    def _add_decision(self, principal, decision_id, kind, text, closes, waives, now):
        self._require_role(principal, "router")
        if not isinstance(decision_id, str) or not _ID_RE.fullmatch(decision_id):
            raise Refused("validation", "decision id must match [A-Za-z0-9._:-]{1,64}")
        if kind not in ("operator", "judge"):
            raise Refused("validation", "kind must be operator or judge")
        body = _text("text", text, required=True)
        closing = _str_list("closes", closes)
        waiving = _str_list("waives", waives)
        with self._write():
            if self.conn.execute("SELECT 1 FROM decisions WHERE id=?",
                                 (decision_id,)).fetchone() is not None:
                raise Refused("conflict", f"decision {decision_id} already recorded")
            self.conn.execute(
                "INSERT INTO decisions(id,ts,actor,kind,text,closes,waives)"
                " VALUES(?,?,?,?,?,?,?)",
                (decision_id, self._iso(now), principal.name, kind, body,
                 _dump(closing), _dump(waiving)))
        return {"id": decision_id, "kind": kind}

    # ------------------------------------------------------------ questions
    def ask_question(self, principal, key, text, default_answer=None, deadline=None,
                     risk_tags=()):
        return self._run(principal, "ask_question", key,
                         lambda now: self._ask_question(principal, key, text,
                                                        default_answer, deadline,
                                                        risk_tags, now))

    def _ask_question(self, principal, key, text, default_answer, deadline, risk_tags,
                      now):
        self._require_role(principal, "router", "l1", "worker", "spawner", "system")
        body = _text("text", text, required=True, key=key)
        default = _text("default_answer", default_answer, key=key)
        if default is not None and not default.strip():
            raise Refused("validation", "default_answer must not be blank")
        tags = _str_list("risk_tags", risk_tags, QUESTION_TAGS, key=key)
        if default is not None and NO_DEFAULT_TAGS.intersection(tags):
            raise Refused("validation",
                          "a risk-tagged question (auth, secrets, money, live-step,"
                          " public-release) has no default")
        due = self._iso_or_none("deadline", deadline)
        if default is not None and due is None:
            raise Refused("validation", "a question with a default needs a deadline")
        with self._write():
            item = self._get_or_refuse(key)
            self._may_act_in(principal, item["project"])
            self._assert_unlocked(item)
            row = self.conn.execute(
                "INSERT INTO questions(id,key,text,default_answer,deadline,risk_tags,"
                "state,created_at)"
                " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM questions),"
                "?,?,?,?,?,'open',?) RETURNING id",
                (key, body, default, due, _dump(tags), self._iso(now))).fetchone()
        return {"id": row[0], "key": key, "state": "open"}

    def answer_question(self, principal, question_id, text=None, source="answer"):
        return self._run(principal, "answer_question", None,
                         lambda now: self._answer_question(principal, question_id,
                                                           text, source, now))

    def _answer_question(self, principal, question_id, text, source, now):
        if source == "answer":
            self._require_role(principal, "router")
            body = _text("text", text, required=True)
        elif source == "question_timeout":
            self._require_role(principal, "router", "system")
            if text is not None:
                raise Refused("validation", "a timeout answer is the stored default;"
                              " pass no text")
            body = None
        else:
            raise Refused("validation", "source must be answer or question_timeout")
        if isinstance(question_id, bool) or not isinstance(question_id, int):
            raise Refused("validation", "question_id must be an integer")
        with self._write():
            row = self.conn.execute(
                "SELECT id,state,default_answer,deadline,risk_tags FROM questions"
                " WHERE id=?", (question_id,)).fetchone()
            if row is None:
                raise Refused("not_found", f"no question {question_id}")
            if row["state"] != "open":
                raise Refused("conflict", f"question {question_id} is {row['state']}")
            if source == "question_timeout":
                if NO_DEFAULT_TAGS.intersection(_load_list(row["risk_tags"])):
                    raise Refused("validation", "a risk-tagged question never times out")
                if row["default_answer"] is None:
                    raise Refused("validation", f"question {question_id} has no default")
                due = self._parse_iso(row["deadline"])
                if due is None or now < due.timestamp():
                    raise Refused("bad_state",
                                  f"question {question_id} has not reached its deadline")
                body = row["default_answer"]
            state = "answered" if source == "answer" else "timeout"
            cursor = self.conn.execute(
                "UPDATE questions SET state=? WHERE id=? AND state='open'",
                (state, question_id))
            if cursor.rowcount != 1:
                raise Refused("conflict", f"question {question_id} changed under the"
                              " transaction")
            self.conn.execute(
                "INSERT INTO answers(question_id,ts,actor,text,source) VALUES(?,?,?,?,?)",
                (question_id, self._iso(now), principal.name, body, source))
        return {"question_id": question_id, "state": state, "source": source,
                "text": body}

    # --------------------------------------------------------------- budget
    def set_budget(self, principal, provider, day, tokens, cost, meter=None):
        return self._run(principal, "set_budget", None,
                         lambda now: self._set_budget(principal, provider, day, tokens,
                                                      cost, meter))

    def _set_budget(self, principal, provider, day, tokens, cost, meter):
        self._require_role(principal, "router", "system")
        if not isinstance(provider, str) or not _PROVIDER_RE.fullmatch(provider):
            raise Refused("validation", "provider must match [A-Za-z0-9._:-]{1,64}")
        try:
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError(day)
        except ValueError:
            raise Refused("validation", "day must be YYYY-MM-DD") from None
        count = _count("tokens", tokens, nullable=False)
        spent = _number("cost", cost, nullable=False)
        reading = _number("meter", meter)
        with self._write():
            self.conn.execute(
                "INSERT INTO budget(provider,day,tokens,cost,meter) VALUES(?,?,?,?,?)"
                " ON CONFLICT(provider,day) DO UPDATE SET tokens=excluded.tokens,"
                "cost=excluded.cost,meter=excluded.meter",
                (provider, day, count, spent, reading))
        return {"provider": provider, "day": day, "tokens": count, "cost": spent,
                "meter": reading}

    # ---------------------------------------------------------------- state
    def set_state(self, principal, key, state, reason=""):
        return self._run(principal, "set_state", key,
                         lambda now: self._set_state(principal, key, state, reason, now))

    def _set_state(self, principal, key, state, reason, now):
        self._require_role(principal, "router", "l1", "system")
        if state not in SETTABLE_STATES:
            raise Refused("validation",
                          f"state must be one of {list(SETTABLE_STATES)}; claim, review,"
                          " done and cancel have their own verbs")
        why = _text("reason", reason or "", cap=500, key=key)
        return self._apply_state(principal, key, state, why, now,
                                 lambda project: self._may_write_for(principal,
                                                                     project))

    def _apply_state(self, principal, key, state, why, now, scope,
                     guard=None, release_claim=False):
        """Move *key* to *state*; *scope(project)* does the authorisation check.

        Idempotent: re-applying the state the item already has changes nothing.
        `guard(item)`, if given, runs inside the write lock after the scope and
        lock checks and may raise; `release_claim` lets a `claimed` item move to
        *state* and clears its claim.
        """
        with self._write():
            item = self._get_or_refuse(key)
            scope(item["project"])
            self._assert_unlocked(item)
            if guard is not None:
                guard(item)
            was = item["state"]
            if was not in SETTABLE_STATES and not (release_claim and was == "claimed"):
                raise Refused("bad_state", f"{key} is {was}; set_state moves only"
                              f" {list(SETTABLE_STATES)}")
            if was == state:
                return {"key": key, "state": state, "changed": False}
            payload = {"state": state, "was": was, "reason": why}
            if release_claim and was == "claimed":
                payload["released"] = True
            self._append(principal, key, "state_set", payload, now)
            if release_claim and was == "claimed":
                cursor = self.conn.execute(
                    "UPDATE items SET state=?, claimed_by=NULL, delegate=NULL,"
                    " reviewer=NULL, lease_until=NULL, review_waiver=NULL,"
                    " updated_at=? WHERE key=? AND state=? AND epoch=?",
                    (state, self._iso(now), key, was, item["epoch"]))
            else:
                cursor = self.conn.execute(
                    "UPDATE items SET state=?, updated_at=? WHERE key=? AND state=?"
                    " AND epoch=?", (state, self._iso(now), key, was, item["epoch"]))
            self._race_guard(cursor, key, was, item["epoch"])
        return {"key": key, "state": state, "changed": True}
