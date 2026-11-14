"""Structured question and answer verbs."""
from .store_card_values import *
from .store_card_values import (_dump, _load_list, _text, _str_list, _count, _number, _exec_parts, _forward, _CARD_KEYS, _CARD_LISTS, _HAND_BACK_STATES)


class CardQuestionsMixin:
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
        self._require_role(principal, "router", "l1", "worker", "spawner", "system", "phase-worker")
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
            if expected_state is not None:
                self._exec_expect_state(item, expected_state, principal, now=now,
                                        claim_checked=principal.role == "worker")
            if principal.role == "worker":
                self._exec_gate(key, {"workflow_id": exec_workflow_id,
                                      "run_id": exec_run_id,
                                      "attempt": exec_attempt}, principal,
                                record=False)
                self._park_guard(principal, item, None, now)
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
        if getattr(principal, "worker_scope", False):
            self._check_principal(principal)
            row = self.conn.execute("SELECT key FROM questions WHERE id=?",
                                    (question_id,)).fetchone()
            if row is not None:
                from .worker_policy import check_project
                check_project(self, principal, row["key"])
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
                "SELECT id,key,state,default_answer,deadline,risk_tags FROM questions"
                " WHERE id=?", (question_id,)).fetchone()
            if row is None:
                raise Refused("not_found", f"no question {question_id}")
            from .worker_policy import check_project
            check_project(self, principal, row["key"])
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
            item = self._get_or_refuse(row["key"])
            self._append(principal, row["key"], "question_answered", {
                "source": source, "question_id": question_id,
                "risk_tag": item.get("risk_tag"), "epoch": item["epoch"]}, now)
        return {"question_id": question_id, "state": state, "source": source,
                "text": body}

    # --------------------------------------------------------------- budget
