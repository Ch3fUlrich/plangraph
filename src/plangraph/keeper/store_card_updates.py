"""Attempt, decision, budget and state updates."""
from .store_card_values import *
from .store_card_values import _ATTEMPT_KEYS, _ATTEMPT_TEXT, _ID_RE, _PROVIDER_RE
from .store_card_values import (_dump, _load_list, _text, _str_list, _count, _number, _exec_parts, _forward, _CARD_KEYS, _CARD_LISTS, _HAND_BACK_STATES)


def _validate_spawn_route(route):
    import subprocess
    from ..fleet_snapshot import _projection_binary

    try:
        result = subprocess.run([_projection_binary(), "validate-attempt-route"],
                                input=json.dumps(route, allow_nan=False), text=True,
                                capture_output=True, timeout=5, check=False)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise Refused("unavailable", "Rust route validator unavailable") from exc
    except ValueError as exc:
        raise Refused("validation", "route must be JSON") from exc
    if result.returncode:
        raise Refused("validation", "invalid spawn attempt route")
    return json.loads(result.stdout)


class CardUpdatesMixin:
    def record_spawn_attempt(self, principal, attempt):
        return self._run(principal, "record_spawn_attempt", None,
                         lambda now: self._record_spawn_attempt(principal, attempt))

    def _record_spawn_attempt(self, principal, attempt):
        from .schema import ATTEMPT_SPAWN_COLUMNS

        self._require_role(principal, "spawner", "system")
        extras = [name for name, _ in ATTEMPT_SPAWN_COLUMNS if name not in ("route", "item_key")]
        text = {name: _text(name, attempt.get(name), cap=512,
                            required=name in ("attempt_id", "role", "host", "plan_hash"))
                for name in (*extras, "harness")}
        if text["role"] not in ("implement", "fix", "review", "orchestrate", "research", "judge"):
            raise Refused("validation", "invalid spawn role")
        if not re.fullmatch(r"[a-f0-9]{64}", text["plan_hash"]):
            raise Refused("validation", "plan_hash must be a SHA-256 digest")
        started = _number("started", attempt.get("started"), nullable=False)
        ended = _number("ended", attempt.get("ended"), nullable=False)
        if started > ended or ended > 253402300799:
            raise Refused("validation", "invalid spawn time range")
        exit_class = attempt.get("exit_class")
        if exit_class not in EXIT_CLASSES:
            raise Refused("validation", "invalid spawn exit class")
        in_tokens = _count("tokens_in", attempt.get("tokens_in"))
        out_tokens = _count("tokens_out", attempt.get("tokens_out"))
        cost = _number("cost", attempt.get("cost"))
        with self._write():
            duplicate = self.conn.execute(
                "SELECT 1 FROM attempts WHERE attempt_id=?", (text["attempt_id"],)).fetchone()
            if duplicate:
                return {"attempt_id": text["attempt_id"], "duplicate": True}
            item = self.resolve_key(text["card_key"]) if text["card_key"] else None
            if item and principal.projects:
                self._require_project(principal, item.partition(":")[0])
            card = item and self.conn.execute("SELECT 1 FROM cards WHERE key=?", (item,)).fetchone()
            text["card_key"] = item if card else None
            columns = (*extras, "item_key", "route", "harness", "model", "class", "run_id",
                       "started_at", "ended_at", "exit_class", "tokens_in", "tokens_out", "cost")
            route = _dump(_validate_spawn_route(attempt.get("route")))
            values = (*(text[name] for name in extras), item, route,
                      text["harness"], text["served_model"], text["task_class"], text["attempt_id"],
                      self._iso(started), self._iso(ended), exit_class, in_tokens, out_tokens, cost)
            marks = ",".join("?" for _ in values)
            self.conn.execute(
                f"INSERT INTO attempts(id,n,{','.join(columns)})"
                f" VALUES((SELECT COALESCE(MAX(id),0)+1 FROM attempts),1,{marks})", values)
        return {"attempt_id": text["attempt_id"], "duplicate": False}

    def spawn_attempts(self, principal, since):
        from datetime import UTC, datetime
        from .cli_verbs import parse_duration

        def read(now):
            try:
                window = parse_duration(since)
            except (ValueError, OverflowError) as exc:
                raise Refused("validation", "since must be a positive duration") from exc
            if window <= 0:
                raise Refused("validation", "since must be a positive duration")
            rows = self.conn.execute(
                "SELECT attempt_id,card_key,COALESCE(item_key,card_key) AS item,role,harness,leg,requested_model,served_model,effort,"
                " level,level_reason,task_class,risk_band,bucket,host,plan_hash,route,"
                " started_at,ended_at,exit_class,tokens_in,tokens_out,cost"
                " FROM attempts WHERE attempt_id IS NOT NULL AND started_at>=?"
                " ORDER BY started_at,attempt_id", (self._iso(max(0, now - window)),))
            projects = principal.projects
            result = []
            for raw in rows:
                row = dict(raw)
                key = row["item"]
                if projects and (key is None or key.partition(":")[0] not in projects):
                    continue
                row.update(item=key or "adhoc", route=json.loads(row["route"]))
                row["wall_s"] = (datetime.fromisoformat(row["ended_at"]).astimezone(UTC)
                                 - datetime.fromisoformat(row["started_at"]).astimezone(UTC)).total_seconds()
                result.append(row)
            return result

        return self._run(principal, "spawn_attempts", None, read)

    def spawn_attempt_coverage(self, principal, day):
        from datetime import UTC, datetime, timedelta

        def read(now):
            self._require_role(principal, "spawner", "system")
            if principal.projects:
                raise Refused("forbidden", "attempt coverage requires all-project access")
            try:
                start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
                end = start + timedelta(days=1)
                if start.date().isoformat() != day:
                    raise ValueError()
            except (TypeError, ValueError, OverflowError) as exc:
                raise Refused("validation", "expected a UTC day as YYYY-MM-DD") from exc
            rows = self.conn.execute(
                "SELECT attempt_id AS run_id, host FROM attempts"
                " WHERE attempt_id IS NOT NULL AND ended_at>=? AND ended_at<?"
                " ORDER BY attempt_id", (self._iso(start.timestamp()), self._iso(end.timestamp())))
            return [dict(row) for row in rows]

        return self._run(principal, "spawn_attempt_coverage", None, read)

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
    def project_state(self, principal, key, state, reason, *, run, run_ord=None):
        """System-only history projection; worker verbs cannot choose the owner."""
        def apply(now):
            self._require_role(principal, "system")
            identity = _exec_parts(key, run)
            if state not in SETTABLE_STATES:
                raise Refused("validation", "projection target must be a settable state")
            return self._apply_state(principal, key, state,
                _text("reason", reason or "", cap=500, key=key), now,
                lambda project: self._may_write_for(principal, project),
                run=identity, projected=True, run_ord=run_ord)
        return self._run(principal, "project_state", key, apply)

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

    def _apply_state(self, principal, key, state, why, now, scope,
                     guard=None, release_claim=False, run=None, new_run=False,
                     hand_back=False, projected=False, run_ord=None):
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
            if projected:
                if was not in SETTABLE_STATES and was != "claimed":
                    raise Refused("bad_state", f"{key} is {was}; projection cannot change ownership")
                if ((item["exec_workflow_id"], item["exec_run_id"]) == run[:2]
                        and was != state and was != "claimed"
                        and not (was in ("waiting", "needs_input") and state == "running")
                        and not _forward(was, state)):
                    raise Refused("bad_transition", f"{key}: {was} -> {state} is not forward")
                self._exec_project(self.conn, item, run, principal, now, run_ord)
                if was == "claimed":
                    return {"key": key, "state": was, "changed": False}
            if was not in SETTABLE_STATES and not (release_claim and was == "claimed"):
                raise Refused("bad_state", f"{key} is {was}; set_state moves only"
                              f" {list(SETTABLE_STATES)}")
            if run is not None and not projected:
                wf, run_id, attempt_no = _exec_parts(key, run)
                if _forward(was, state):
                    self._exec_check(self.conn, key, wf, run_id, attempt_no,
                                     principal=principal, now=now,
                                     record=was != state)
                else:
                    raise Refused("bad_transition",
                                  f"{key}: {was} -> {state} is not a forward move")
            elif (not projected and item["exec_run_id"] is not None and was != state
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
