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
  applies the default once the deadline has passed.  RT-P1-EXEC-OWNERSHIP: an ask
  carrying ``(workflow_id, run_id, attempt)`` is checked against the card's recorded
  exec owner exactly like a run-carrying ``set_state`` move; a worker/spawner that
  omits them on an owned card is refused.
* ``set_budget``: absolute tokens/cost/meter for one provider and day.
* ``set_state``: move an item among the non-claim states (open, ready, running,
  needs_input, waiting, failed, parked); claim, review, done and cancel keep their verbs.
  RT-P1-EXEC-OWNERSHIP: a move that carries ``run=(workflow_id, run_id, attempt)`` is
   exec-owned -- it must stay forward per ``TRANSITIONS``, and the recorded
  owner refuses a stale run or an attempt that would go backwards.  A hand-back
  is EXPLICIT: ``set_state(..., hand_back=True)`` that changes the state and
  lands on ``open``/``ready`` frees the exec owner for the next run.  A bare
  run-less move (the projector/planner write path) never clears the owner, so a
  run-less write cannot free a live run's card (RT-P1 V1-B); on an OWNED card it
  must also stay forward -- a bare back move is ``bad_transition`` (RT-P1 sq5
  fix 3).  Owners are recorded only by a worker principal and are bound to it
  (sq5 fix 1), and every clear keeps a retired-run fence (sq5 fix 2).
"""
from __future__ import annotations

import json
import math
import re
from datetime import date

from plangraph.card_paths import normalise_path, path_problem

from .credential_guard import guard
from .errors import Refused
from .events import STATE_EVENT_TYPES
from .schema import EXIT_CLASSES, RISK_TAGS, WINDOW_CLASSES

__all__ = ["CardsMixin", "SETTABLE_STATES", "TRANSITIONS", "NO_DEFAULT_TAGS"]

#: States ``set_state`` moves between (none of them carries a claim, lease or epoch).
SETTABLE_STATES = ("open", "ready", "running", "needs_input", "waiting", "failed",
                   "parked")

#: RT-P1-EXEC-OWNERSHIP: the hand-back states.  An EXPLICIT ``set_state`` move
#: (``hand_back=True``) that lands on one of these and changes the state is a
#: router/operator hand-back: it frees the exec owner so the next run can claim
#: the card (same transaction as the state UPDATE).  A bare run-less move -- the
#: projector/planner write path -- is never a hand-back and keeps the owner; so
#: do run-carrying moves, no-op moves and moves to any other state.
_HAND_BACK_STATES = frozenset({"open", "ready"})

#: RT-P1-EXEC-OWNERSHIP: direct successors of each settable state (same-state always
#: allowed; reachability is the transitive closure).  A set-state move that carries a
#: ``run`` may only follow these edges forward; the back-moves (``failed``/``parked``
#: back to ``open``/``ready``) are refused. A run-carrying move never clears
#: ownership; an explicit hand-back (``hand_back=True``) to ``open``/``ready``
#: does (see ``_HAND_BACK_STATES``).
#: Moves without a ``run``
#: keep their historical behaviour. Worker parks do not consult this table at
#: all: ``_worker_park`` applies its state move with no ``run``, so park never
#: raises ``bad_transition`` (it refuses ``bad_state`` instead).
TRANSITIONS: dict[str, frozenset[str]] = {
    "open":        frozenset({"open", "ready"}),
    "ready":       frozenset({"ready", "running"}),
    "running":     frozenset({"running", "needs_input", "waiting"}),
    "needs_input": frozenset({"needs_input", "waiting", "parked", "failed"}),
    "waiting":     frozenset({"waiting", "needs_input", "parked", "failed"}),
    "failed":      frozenset({"failed", "parked"}),
    "parked":      frozenset({"parked", "failed"}),
}

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


def _exec_parts(key, run):
    """Validate a ``run=(workflow_id, run_id, attempt)``; return the three parts.

    Every part must be present and well-formed whether or not the item already
    has a recorded owner: recording a malformed owner would poison the card.
    """
    if not isinstance(run, (tuple, list)) or len(run) != 3:
        raise Refused("validation",
                      "run must be a (workflow_id, run_id, attempt) tuple")
    wf, run_id, attempt = run
    wf = _text("workflow_id", wf, required=True, cap=200, key=key)
    run_id = _text("run_id", run_id, required=True, cap=200, key=key)
    if (isinstance(attempt, bool) or not isinstance(attempt, int)
            or not 1 <= attempt <= 2**31 - 1):
        raise Refused("validation", "attempt must be an integer in [1, 2147483647]")
    return wf, run_id, attempt


def _forward(was, state):
    """True when *state* is *was* or forward-reachable from it per TRANSITIONS."""
    todo, seen = [was], set()
    while todo:
        here = todo.pop()
        if here == state:
            return True
        for nxt in TRANSITIONS.get(here, frozenset()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return False


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
                     risk_tags=(), exec_workflow_id=None, exec_run_id=None,
                     exec_attempt=None, expected_state=None):
        return self._run(principal, "ask_question", key,
                         lambda now: self._ask_question(principal, key, text,
                                                        default_answer, deadline,
                                                        risk_tags, now,
                                                        exec_workflow_id,
                                                        exec_run_id, exec_attempt,
                                                        expected_state))

    def _ask_question(self, principal, key, text, default_answer, deadline, risk_tags,
                      now, exec_workflow_id=None, exec_run_id=None, exec_attempt=None,
                      expected_state=None):
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
            # Compare the observed state before _exec_check can record an owner.
            if expected_state is not None:
                self._exec_expect_state(item, expected_state, principal, now=now,
                                        claim_checked=principal.role == "worker")
            if principal.role == "worker":
                self._exec_gate(key, {"workflow_id": exec_workflow_id,
                                      "run_id": exec_run_id,
                                      "attempt": exec_attempt}, principal,
                                record=False)
                self._park_guard(principal, item, None, now)
            # RT-P1-EXEC-OWNERSHIP: an ask carrying any exec field is checked
            # against the recorded owner (a first ask records it; a stale run
            # or a backwards attempt is refused and inserts no question).  A
            # worker/spawner that omits all three on an owned card names no
            # run, so it is refused as exec_stale; router/l1/system callers
            # (planner, router) never are.
            if any(part is not None
                   for part in (exec_workflow_id, exec_run_id, exec_attempt)):
                self._exec_check(self.conn, key, exec_workflow_id, exec_run_id,
                                 exec_attempt, principal=principal, now=now)
            elif principal.role in ("worker", "spawner"):
                row = self.conn.execute(
                    "SELECT exec_run_id FROM items WHERE key=?", (key,)).fetchone()
                if row is not None and row["exec_run_id"] is not None:
                    raise Refused("exec_stale",
                                  f"{key} has an exec owner: workflow_id, run_id"
                                  " and attempt are required")
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
    def set_state(self, principal, key, state, reason="", run=None, new_run=False,
                  hand_back=False):
        # Only ``hand_back=True`` hands the card back: on a changing move to
        # open/ready it clears the exec owner (``_HAND_BACK_STATES``) so the
        # next run can claim it.  A bare run-less move -- the projector/planner
        # write path -- keeps the owner (RT-P1 V1-B).
        return self._run(principal, "set_state", key,
                         lambda now: self._set_state(principal, key, state, reason,
                                                     now, run=run, new_run=new_run,
                                                     hand_back=hand_back))

    def _set_state(self, principal, key, state, reason, now, run=None, new_run=False,
                    hand_back=False):
        self._require_role(principal, "router", "l1", "system")
        if hand_back:
            self._require_role(principal, "router", "system")
        if new_run:
            raise Refused("validation", "new_run is removed; use requeue")
        if hand_back:
            if run is not None or state not in _HAND_BACK_STATES:
                raise Refused("validation", "hand_back requires no run and an open/ready target")
        if state not in SETTABLE_STATES:
            raise Refused("validation",
                          f"state must be one of {list(SETTABLE_STATES)}; claim, review,"
                          " done and cancel have their own verbs")
        why = _text("reason", reason or "", cap=500, key=key)
        return self._apply_state(principal, key, state, why, now,
                                 lambda project: self._may_write_for(principal,
                                                                     project),
                                 run=run, new_run=new_run, hand_back=hand_back)

    def requeue(self, principal, key, reason=""):
        """Router-only revival of a parked card, retiring its previous run."""
        def apply(now):
            self._require_role(principal, "router")
            def guard(item):
                if item["state"] != "parked":
                    raise Refused("bad_state", "requeue requires a parked card")
            return self._apply_state(principal, key, "open",
                                     _text("reason", reason or "", cap=500, key=key),
                                     now, lambda project: self._may_write_for(principal, project),
                                     guard=guard, hand_back=True)
        return self._run(principal, "requeue", key, apply)

    def _exec_clear(self, conn, key, principal, now):
        """Free the exec owner inside the caller's keeper transaction.

        RT-P1 sq5 fix 2 (V1-1/V5-1): the clear RETIRES the run it frees -- the
        ``(workflow_id, run_id, max attempt)`` triple joins the card's
        retired-run fence (``exec_retired_runs``), kept in the same UPDATE that
        drops the owner, so a late call from that run is refused ``exec_stale``
        and cannot reacquire the card as its first writer.  Every clear door
        (reap, release, both review changes, reopen, withdraw and the explicit
        ``set_state`` hand-back) goes through here, so the fence is written for
        all of them at once.

        RT-P1 sq12 fix 1: the fence NEVER FORGETS -- every retired run stays
        in ``exec_retired_runs`` with no cap, so a run retired arbitrarily
        many clears ago is still refused.  (The earlier ``retired[-16:]``
        bound evicted still-live identities after 17 clears.)
        """
        row = conn.execute(
            "SELECT exec_workflow_id, exec_run_id, exec_attempt, exec_retired_runs"
            " FROM items WHERE key=?", (key,)).fetchone()
        if row is None or row["exec_run_id"] is None:
            # Nothing to retire: an unowned card only drops the (already NULL)
            # triple and keeps whatever fence earlier clears left behind.
            conn.execute("UPDATE items SET exec_workflow_id=NULL, exec_run_id=NULL,"
                         " exec_attempt=NULL WHERE key=?", (key,))
            return
        retired = [entry for entry in _load_list(row["exec_retired_runs"])
                   if isinstance(entry, dict)]
        by_run = [(entry.get("workflow_id"), entry.get("run_id"))
                  for entry in retired]
        run_key = (row["exec_workflow_id"], row["exec_run_id"])
        if run_key in by_run:
            entry = retired[by_run.index(run_key)]
            entry["attempt"] = max(entry.get("attempt") or 0,
                                   row["exec_attempt"] or 0)
        else:
            retired.append({"workflow_id": run_key[0], "run_id": run_key[1],
                            "attempt": row["exec_attempt"]})
        conn.execute("UPDATE items SET exec_workflow_id=NULL, exec_run_id=NULL,"
                     " exec_attempt=NULL, exec_principal=NULL, exec_retired_runs=? WHERE key=?",
                     (_dump(retired), key))
        self._append(principal, key, "exec_owner_changed",
                     {"action": "clear", "retired": retired}, now)

    def _exec_record(self, conn, item_id, wf, run, attempt, principal=None, now=None):
        """Record the exec owner of *item_id*: the first owner of an unowned card.

        Only ``_exec_check`` calls this, and only while no owner is recorded, so
        an existing owner is never replaced here.

        RT-P1 sq5 fix 1 (V2-1): the owner is stored WITH the principal that
        presented the triple (``exec_principal``), and only a worker principal
        may record one -- a router/l1/system/spawner token planting a triple on
        an unowned card is refused, never recorded.
        """
        if principal is None or getattr(principal, "role", None) != "worker":
            name = getattr(principal, "name", None) or "<none>"
            raise Refused("forbidden",
                          f"{item_id}: only the worker principal may record an"
                          f" exec owner, not {name}")
        wf, run, attempt = _exec_parts(item_id, (wf, run, attempt))
        touch = ", updated_at=?" if now is not None else ""
        values = (wf, run, attempt, principal.name)
        if now is not None:
            values += (self._iso(now),)
        conn.execute("UPDATE items SET exec_workflow_id=?, exec_run_id=?,"
                     f" exec_attempt=?, exec_principal=?{touch} WHERE key=?",
                      (*values, item_id))
        self._append(principal, item_id, "exec_owner_changed",
                     {"action": "record", "workflow_id": wf, "run_id": run,
                      "attempt": attempt}, now if now is not None else self.clock())

    def _exec_check(self, conn, item_id, wf, run, attempt, principal=None,
                    now=None, record=True):
        """RT-P1-EXEC-OWNERSHIP: enforce the recorded exec owner of *item_id*.

        An unowned card records ``(wf, run, attempt)`` -- but only from a worker
        principal (sq5 fix 1, V2-1), bound to that principal, and never for a run
        the card's retired-run fence holds (sq5 fix 2, V1-1/V5-1): a retired run
        is refused ``exec_stale`` whether or not the card has an owner, so a
        clear can never be reacquired by the run it retired.  The same run may
        only move its attempt forward (never backwards); a different run is
        stale; a worker other than the owner's principal is refused ``forbidden``
        (the stale/lower-attempt checks decide first, as everywhere).
        Runs inside the caller's write lock; callers pass ``conn`` (== ``self.conn``)
        so the helper reads as a pure check against one connection.
        """
        wf, run, attempt = _exec_parts(item_id, (wf, run, attempt))
        row = conn.execute(
            "SELECT exec_workflow_id, exec_run_id, exec_attempt, exec_principal,"
            " exec_retired_runs, claimed_by, delegate FROM items WHERE key=?",
            (item_id,)).fetchone()
        recorded = row["exec_run_id"] if row is not None else None
        retired = [(entry.get("workflow_id"), entry.get("run_id"))
                   for entry in _load_list(row["exec_retired_runs"] or "[]")
                   if isinstance(entry, dict)] if row is not None else []
        is_owner = (recorded is not None
                    and (wf, run) == (row["exec_workflow_id"], recorded))
        if not is_owner and (wf, run) in retired:
            # The fence: whatever the card's state now, this run is done here.
            raise Refused("exec_stale",
                          f"{item_id}: run {run!r} is retired by an owner clear"
                          " and cannot reacquire the card")
        if recorded is None:
            if not record:
                return
            if principal is None or getattr(principal, "role", None) != "worker":
                name = getattr(principal, "name", None) or "<none>"
                raise Refused("forbidden",
                              f"{item_id}: only the worker principal may record"
                              f" an exec owner, not {name}")
            holder = row["claimed_by"] if row is not None else None
            if holder is None or holder != principal.name:
                raise Refused("forbidden",
                              f"{item_id} is held by {holder}: only the holder"
                              " may record its exec owner")
            self._exec_record(conn, item_id, wf, run, attempt, principal, now=now)
            return
        if run != recorded or wf != row["exec_workflow_id"]:
            raise Refused("exec_stale",
                          f"{item_id} is owned by run {recorded!r}, not {run!r}")
        at = row["exec_attempt"] or 0
        if attempt < at:
            raise Refused("exec_stale",
                          f"{item_id} is at attempt {at}; {attempt} would go backwards")
        owner_principal = row["exec_principal"]
        if (principal is not None and owner_principal is not None
                and principal.role in ("worker", "spawner")
                and principal.name != owner_principal):
            raise Refused("forbidden",
                          f"{item_id} is owned for {owner_principal}, not"
                          f" {principal.name}")
        if attempt > at and record:
            touch = ", updated_at=?" if now is not None else ""
            values = (attempt,) if now is None else (attempt, self._iso(now))
            conn.execute(f"UPDATE items SET exec_attempt=?{touch} WHERE key=?",
                         (*values, item_id))
            self._append(principal, item_id, "exec_owner_changed",
                         {"action": "advance", "workflow_id": wf, "run_id": run,
                          "attempt": attempt}, now if now is not None else self.clock())

    def _exec_state_after_owner(self, conn, item_id):
        """True when the card's state was written after its exec owner was recorded.

        RT-P1 sq12 fix 2 (V3-A): the narrowest ordering signal the schema gives
        is ``events.seq`` -- the append-only log's monotonic sequence, assigned
        by ``append_event`` itself so both backends number identically and a
        rollback never leaves a gap.  ``owner_seq`` is the newest
        ``exec_owner_changed`` ``record``/``advance`` (a record or an attempt
        advance is when the current attempt began); ``state_seq`` is the newest
        event whose replay writes ``state`` (``events.STATE_EVENT_TYPES``).  A
        state write newer than the owner record means someone moved the card
        since the attempt began -- in practice the projector/planner write
        path, whose state a delayed worker retry must not roll back.

        A record/advance event is the only observable moment the current
        attempt began, so without one this is False: production always records
        through ``_exec_check``/``_exec_record``, while a directly seeded owner
        (test setup) has no event at all -- with no observable attempt start the
        narrowest signal says nothing and the park lands as today.
        """
        owner_seq = state_seq = None
        for event in conn.execute(
                "SELECT seq, type, payload FROM events WHERE key=?"
                " ORDER BY seq DESC", (item_id,)):
            kind = event["type"]
            if state_seq is None and kind in STATE_EVENT_TYPES:
                state_seq = event["seq"]
            if owner_seq is None and kind == "exec_owner_changed":
                payload = json.loads(event["payload"] or "{}")
                if payload.get("action") in ("record", "advance"):
                    owner_seq = event["seq"]
            if owner_seq is not None and state_seq is not None:
                break
        return (owner_seq is not None and state_seq is not None
                and state_seq > owner_seq)

    def _apply_state(self, principal, key, state, why, now, scope,
                     guard=None, release_claim=False, run=None, new_run=False,
                     hand_back=False):
        """Move *key* to *state*; *scope(project)* does the authorisation check.

        Idempotent: re-applying the state the item already has changes nothing.
        `guard(item)`, if given, runs inside the write lock after the scope and
        lock checks and may raise; `release_claim` lets a `claimed` item move to
        *state* and clears its claim.  RT-P1-EXEC-OWNERSHIP: a move carrying
        ``run=(workflow_id, run_id, attempt)`` is exec-owned -- it must be
        forward per ``TRANSITIONS`` (back-moves are always refused),
        and the recorded owner refuses a stale run or a backwards attempt.
        A hand-back is EXPLICIT: ``hand_back=True`` on a move that changes the
        state, lands on ``open``/``ready`` and carries no ``run`` (a
        router/operator move -- neither a worker park nor a projector/planner
        write) clears the exec owner in the same transaction as the state
        UPDATE (``_HAND_BACK_STATES``), so the next run can claim the card.  A
        bare run-less move is never a hand-back: a projector/planner write to
        ``open``/``ready`` keeps the owner of a live run (RT-P1 V1-B).  A bare
        run-less BACK move on an OWNED card is refused ``bad_transition``
        (sq5 fix 3): the forward table is not for run-carrying callers only any
        more, so no admin/projector write can walk an owned card backwards and
        strand it -- the explicit hand-back door (or ``release_claim``) is the
        way back.  Unowned cards keep the historical run-less behaviour.
        No-op moves, run-carrying moves and moves to any other state keep the owner.
        ``new_run`` is refused by set_state. Callers without
        ``run`` keep the historical, unchecked behaviour when no owner is recorded.
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
            if run is not None:
                wf, run_id, attempt_no = _exec_parts(key, run)
                if _forward(was, state):
                    self._exec_check(self.conn, key, wf, run_id, attempt_no,
                                     principal=principal, now=now,
                                     record=was != state)
                else:
                    raise Refused("bad_transition",
                                  f"{key}: {was} -> {state} is not a forward move")
            elif (item["exec_run_id"] is not None and was != state
                  and not hand_back and not release_claim
                  and not (was in ("waiting", "needs_input") and state == "running")
                  and not _forward(was, state)):
                # RT-P1 sq5 fix 3 (deepseek V1): the forward table also guards a
                # bare run-less move on an OWNED card, so router/l1/system cannot
                # walk an owned card backwards and strand it open/ready-but-owned.
                raise Refused("bad_transition",
                              f"{key}: {was} -> {state} is not a forward move and"
                              f" {key} is owned; a run-less move must stay forward"
                              " (hand_back=True is the way back)")
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
            if hand_back and run is None and state in _HAND_BACK_STATES:
                # RT-P1-EXEC-OWNERSHIP: the explicit hand-back door frees the
                # owner for the next run, in the same transaction as the move
                # above; a bare run-less move never reaches this line (V1-B).
                self._exec_clear(self.conn, key, principal, now)
        return {"key": key, "state": state, "changed": True}
