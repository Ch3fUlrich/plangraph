You have NO tools and no repository access. The full material is inline below. Answer in plain text; never emit tool calls.

Review the diff below (keeper inbox: show and acknowledge open projector alarms; reads scoped per project like questions). Vectors:
V1 Ack storage: ack is the successful (ok=1) audit_log row action 'ack_alarm', key 'alarm:<id>' written by Store._run on success. Can a refused/failed ack hide an alarm? Can another path forge such a row?
V2 Authz on write: ack_alarm only router/system. Any bypass (run tokens, bool/str/negative ids, other roles such as l1/worker/reviewer/reader)?
V3 Authz on read (scoping): router/system/reader see all; other roles only alarms whose key project (key.split(':')[0]) is in principal.projects; keyless alarms router/system/reader only. Find any leak (empty projects, key without colon, odd keys, run principals) and any case where scoping is applied after the error path or skipped.
V4 Listing correctness: cleared never shown, acked never re-shown, re-raised alarm (new id) shown again; unreadable table/query error yields an error field, never a silent empty list.
V5 SQL portability sqlite+postgres (|| CAST(id AS TEXT), NOT EXISTS) and no schema change/DDL.
V6 form.py page: XSS (textContent only), CSP compat, error banner on failure.
V7 Tests: do they fail without the change and cover foreign-project principal seeing nothing and non-router ack refused 403?

=== DIFF
diff --git a/docs/contract/keeper-contract-v1-inbox.md b/docs/contract/keeper-contract-v1-inbox.md
index 95856ab..89172d6 100644
--- a/docs/contract/keeper-contract-v1-inbox.md
+++ b/docs/contract/keeper-contract-v1-inbox.md
@@ -44,6 +44,44 @@ Router or system only (else 403). Body `{question_id}`. The question must be
 stored answer and returns `{"question_id", "signal": {...}}`. Audited as
 `resignal_question`.
 
+## `GET /v1/alarms`
+
+Any authenticated principal, audited as `alarms` like the other reads, with the
+same visibility rule as `GET /v1/questions`: `router`, `system` and `reader` see
+every project's alarms, other roles only the alarms whose `key` project (the part
+before the first `:`) is in their principal's projects — so a keyless alarm (an
+empty `key`, which has no project) is visible to `router`, `system` and `reader`
+only. Lists the projector alarms that are still open: `projector_alarms` rows with
+`cleared_at IS NULL` that have no successful `ack_alarm` audit row
+(`audit_log.action = 'ack_alarm'`, `ok = 1`, `key = 'alarm:<id>'`), ordered by `id`.
+
+```json
+{"alarms": [{"id": 4, "key": "demo:DEMO-1", "kind": "state_drift",
+  "workflow_id": "wf-1", "detail": "...", "raised_at": "2026-01-01T00:00:00Z",
+  "age_s": 90}]}
+```
+
+`age_s` is whole seconds from `raised_at` to `store.now()`, or `null` when the
+timestamp cannot be parsed. When the query itself fails (a missing or unreadable
+table), the reply is still `200` but carries
+`{"alarms": [], "error": "alarms unreadable: <ExceptionClass>"}` — never a silently
+empty list, and never message text or a path.
+
+## `POST /v1/ack_alarm`
+
+Router or system only (else 403). Body `{alarm_id}`: an integer, not a boolean or
+string (else `validation`). The alarm must exist (`not_found`) and must not be
+cleared (`bad_state`); acknowledging an already acknowledged alarm is idempotent
+(a second audit row, no error). The acknowledgement is stored as the successful
+`ack_alarm` audit_log row keyed `alarm:<id>` that the store writes on success —
+no schema change. A refusal is audited with `ok = 0` and does not hide the
+alarm. The reply is `{"alarm_id", "acked": true}`.
+
+## Acknowledgement storage
+
+The acknowledgement is the successful `ack_alarm` `audit_log` row keyed
+`alarm:<id>`; no new table, no new column, no migration — the schema is unchanged.
+
 ## `GET /inbox`
 
 Static, unauthenticated HTML form (not under `/v1`). Served with
diff --git a/src/plangraph/inbox/form.py b/src/plangraph/inbox/form.py
index 263b80d..359590e 100644
--- a/src/plangraph/inbox/form.py
+++ b/src/plangraph/inbox/form.py
@@ -10,6 +10,7 @@ PAGE = """<!doctype html>
 *{box-sizing:border-box}
 body{margin:0 auto;max-width:42rem;padding:1rem;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif}
 h1{font-size:1.3rem;margin:0 0 .75rem}
+h2{font-size:1.05rem;margin:1rem 0 .5rem}
 .row{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.75rem}
 input,textarea,button{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--bd);border-radius:6px;padding:.5rem}
 input{flex:1;min-width:10rem}
@@ -31,6 +32,8 @@ textarea{width:100%;min-height:5rem;margin:.5rem 0}
 </div>
 <div id="msg" class="mut"></div>
 <div id="list"></div>
+<h2>Alarms</h2>
+<div id="alarms"></div>
 <script>
 (function(){
 var token="";
@@ -69,6 +72,32 @@ function card(q){
   c.appendChild(ta);c.appendChild(btn);c.appendChild(st);
   return c;
 }
+function alarmCard(a){
+  var c=el("div","card"),btn=el("button",null,"Acknowledge"),st=el("div","mut");
+  c.appendChild(el("div","key",a.key));
+  c.appendChild(el("div","mut",a.kind+" / "+a.workflow_id+" - age "+(a.age_s==null?"?":a.age_s)+"s"));
+  btn.addEventListener("click",function(){
+    btn.disabled=true;st.className="mut";st.textContent="acknowledging...";
+    fetch("/v1/ack_alarm",{method:"POST",headers:hdr(true),body:JSON.stringify({alarm_id:a.id})})
+    .then(function(r){return r.json().catch(function(){return {}}).then(function(b){
+      if(r.ok&&b.acked){c.remove();msg("acknowledged "+a.key,"ok")}
+      else{var t="error: "+((b&&(b.msg||b.error))||("HTTP "+r.status));
+        st.className="err";st.textContent=t;msg(t,"err");btn.disabled=false}
+    })})
+    .catch(function(e){st.className="err";st.textContent="error: "+e.message;btn.disabled=false});
+  });
+  c.appendChild(btn);c.appendChild(st);
+  return c;
+}
+function loadAlarms(){
+  fetch("/v1/alarms",{headers:hdr(false)})
+  .then(function(r){return r.json().catch(function(){return {}}).then(function(b){
+    var box=$("alarms");box.textContent="";
+    if(!r.ok||b.error){msg("error: "+((b&&(b.msg||b.error))||("HTTP "+r.status)),"err");return}
+    (Array.isArray(b.alarms)?b.alarms:[]).forEach(function(a){box.appendChild(alarmCard(a))});
+  })})
+  .catch(function(e){msg("error: "+e.message,"err")});
+}
 function load(){
   token=$("tok").value;
   if(!token){msg("enter a token first","err");return}
@@ -80,6 +109,7 @@ function load(){
     l.textContent="";
     qs.forEach(function(q){l.appendChild(card(q))});
     msg(qs.length+" open question(s)");
+    loadAlarms();
   })})
   .catch(function(e){msg("error: "+e.message,"err")});
 }
diff --git a/src/plangraph/inbox/routes.py b/src/plangraph/inbox/routes.py
index f64b98c..cb9a51e 100644
--- a/src/plangraph/inbox/routes.py
+++ b/src/plangraph/inbox/routes.py
@@ -7,27 +7,46 @@ Temporal signal for an already answered (or timed-out) question.  An answer send
 its signal only after the store commit, and a signal failure never fails or rolls
 back the answer.  The keeper runs no deadline timer: that lives in Temporal.
 
+``GET /v1/alarms`` lists the open (uncleared, unacknowledged) projector alarms the
+caller may see — the same project scoping as ``GET /v1/questions`` (keyless alarms
+are router/system/reader only) — and ``POST /v1/ack_alarm`` acknowledges one for
+good (router or system only).  The
+acknowledgement *is* the audit row: the successful ``ack_alarm`` ``audit_log`` entry
+keyed ``alarm:<id>`` that ``Store._run`` writes on success, so there is no separate
+acks table and no schema change; a refusal is audited ``ok=0`` and hides nothing.
+
 All handlers run after bearer auth and ``api_routes.run_gate`` in ``api.py``.
 """
 from __future__ import annotations
 
 import json
 
+from ..keeper import db_errors
 from ..keeper.errors import Refused
 from . import temporal_signal
 
 __all__ = ["READ_VERBS", "WRITE_VERBS", "PAGE_HEADERS", "page", "read", "write"]
 
-READ_VERBS = {"questions"}
-WRITE_VERBS = {"ask_question", "answer_question", "resignal_question"}
+READ_VERBS = {"questions", "alarms"}
+WRITE_VERBS = {"ask_question", "answer_question", "resignal_question", "ack_alarm"}
 
 _STATES = ("open", "answered", "timeout", "all")
-#: Roles that see every project's questions; all others see their own projects.
+#: Roles that see every project's questions and alarms; all others see their own.
 _SEES_ALL = ("router", "system", "reader")
 _SELECT = ("SELECT q.id, q.key, q.text, q.default_answer, q.deadline, q.risk_tags,"
            " q.state, q.created_at, a.ts AS a_ts, a.actor AS a_actor,"
            " a.text AS a_text, a.source AS a_source"
            " FROM questions q LEFT JOIN answers a ON a.question_id = q.id")
+#: Open projector alarms nobody has acknowledged yet: no ``ack_alarm`` audit row
+#: (``ok=1``) for ``alarm:<id>`` (portable SQL, sqlite and postgres alike).
+_ALARMS = ("SELECT id, key, kind, workflow_id, detail, raised_at FROM projector_alarms"
+           " WHERE cleared_at IS NULL"
+           " AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.action = 'ack_alarm'"
+           " AND a.ok = 1 AND a.key = 'alarm:' || CAST(projector_alarms.id AS TEXT))"
+           " ORDER BY id")
+#: Every database failure an alarms read can take (a missing table included): the
+#: caller gets a named error instead of a silently empty list.
+_UNREADABLE = db_errors.NO_TABLE + db_errors.OPERATIONAL + db_errors.PROGRAMMING + db_errors.DENIED
 
 
 #: Headers for ``GET /inbox``: a static page that may only call its own origin.
@@ -108,6 +127,31 @@ def _list(store, principal, query: dict) -> dict:
     return {"questions": store._run(principal, "questions", None, fn)}
 
 
+def _age_s(store, stamp, moment) -> int | None:
+    """Whole seconds between the ISO *stamp* and *moment*; ``None`` if unparsable."""
+    when = store._parse_iso(stamp)
+    return None if when is None else int(moment - when.timestamp())
+
+
+def _alarms(store, principal) -> dict:
+    """The open, unacknowledged projector alarms *principal* may see, oldest first."""
+    def fn(now):
+        try:
+            rows = store.conn.execute(_ALARMS).fetchall()
+        except _UNREADABLE as exc:      # table missing or unreadable: named, never silent
+            return {"alarms": [], "error": f"alarms unreadable: {type(exc).__name__}"}
+        moment = store.now()
+        allowed = None if principal.role in _SEES_ALL else set(principal.projects or [])
+        return {"alarms": [{"id": r["id"], "key": r["key"], "kind": r["kind"],
+                            "workflow_id": r["workflow_id"], "detail": r["detail"],
+                            "raised_at": r["raised_at"],
+                            "age_s": _age_s(store, r["raised_at"], moment)}
+                           for r in rows
+                           if allowed is None or _project(r["key"]) in allowed]}
+
+    return store._run(principal, "alarms", None, fn)
+
+
 def _stored_answer(store, question_id) -> dict | None:
     """The stored answer of *question_id* as a signal request, or ``None``."""
     row = store.conn.execute(
@@ -165,10 +209,35 @@ def _resignal(store, principal, body: dict) -> dict:
     return store._run(principal, "resignal_question", row["key"] if row else None, fn)
 
 
+def _ack(store, principal, body: dict) -> dict:
+    aid = body.get("alarm_id")
+    key = (f"alarm:{aid}" if isinstance(aid, int) and not isinstance(aid, bool)
+           else None)                       # the audit key, before _run (like _resignal)
+
+    def fn(now):
+        if principal.role not in ("router", "system"):
+            raise Refused("forbidden", "ack_alarm is router or system only")
+        if isinstance(aid, bool) or not isinstance(aid, int):
+            raise Refused("validation", "alarm_id must be an integer")
+        row = store.conn.execute("SELECT cleared_at FROM projector_alarms WHERE id=?",
+                                 (aid,)).fetchone()
+        if row is None:
+            raise Refused("not_found", f"no alarm {aid}")
+        if row["cleared_at"] is not None:
+            raise Refused("bad_state", f"alarm {aid} is cleared")
+        # the ack is the ``ok=1`` audit row ``_run`` writes for this key on success;
+        # a repeat ack just adds another one and stays idempotent.
+        return {"alarm_id": aid, "acked": True}
+
+    return store._run(principal, "ack_alarm", key, fn)
+
+
 def read(store, principal, name: str, query: dict) -> dict:
     """Dispatch a GET inbox verb (``query`` is the ``parse_qs`` mapping)."""
     if name == "questions":
         return _list(store, principal, query)
+    if name == "alarms":
+        return _alarms(store, principal)
     raise Refused("not_found", f"no read verb {name!r}")
 
 
@@ -180,4 +249,6 @@ def write(store, principal, name: str, body: dict) -> dict:
         return _answer(store, principal, body)
     if name == "resignal_question":
         return _resignal(store, principal, body)
+    if name == "ack_alarm":
+        return _ack(store, principal, body)
     raise Refused("not_found", f"no write verb {name!r}")
diff --git a/tests/test_inbox_alarms.py b/tests/test_inbox_alarms.py
new file mode 100644
index 0000000..fe60902
--- /dev/null
+++ b/tests/test_inbox_alarms.py
@@ -0,0 +1,164 @@
+"""``GET /v1/alarms`` and ``POST /v1/ack_alarm``: the projector alarms inbox doors.
+
+Fake ``projector_alarms`` rows are written straight into the keeper, then read
+and acknowledged over HTTP with the same ``World``/``Doors`` helpers the other
+inbox tests use, so the file runs unchanged under ``PG_KEEPER_BACKEND`` (except
+the deliberate table drop, which needs a raw SQLite handle).
+"""
+import sqlite3
+
+import pytest
+
+from door_util import Doors
+from keeper_util import World
+from plangraph.keeper import db as kdb
+from plangraph.inbox.form import PAGE
+
+RAISED = ("INSERT INTO projector_alarms(id, kind, workflow_id, key, detail, raised_at,"
+          " cleared_at) VALUES(?,?,?,?,?,?,?)")
+needs_sqlite = pytest.mark.skipif(
+    kdb.select_backend() != "sqlite", reason="the table is dropped on a raw SQLite handle")
+
+
+@pytest.fixture
+def w(tmp_path):
+    world = World(tmp_path)
+    try:
+        yield world
+    finally:
+        world.close()
+
+
+@pytest.fixture
+def doors(w, tmp_path):
+    live = Doors(w, tmp_path)
+    try:
+        yield live
+    finally:
+        live.close()
+
+
+def raise_alarm(w, alarm_id=1, kind="state_drift", workflow_id="card:A", key="demo:A",
+                cleared_at=None, detail="drifted"):
+    stamp = w.store._iso(w.clock())
+    w.conn.execute(RAISED, (alarm_id, kind, workflow_id, key, detail, stamp, cleared_at))
+    return alarm_id
+
+
+def listed(doors, who="router"):
+    status, body = doors.http(who, "GET", "/v1/alarms")
+    assert status == 200, body
+    return body
+
+
+def ack(doors, alarm_id, who="router"):
+    return doors.http(who, "POST", "/v1/ack_alarm", {"alarm_id": alarm_id})
+
+
+# ------------------------------------------------------------------- reads
+def test_lists_open_alarms_with_key_kind_and_age(doors, w):
+    raise_alarm(w)
+    raise_alarm(w, alarm_id=2, key="demo:B", cleared_at=w.store._iso(w.clock()))
+    w.clock.advance(45)
+    body = listed(doors)
+    assert [row["id"] for row in body["alarms"]] == [1]          # the cleared row never lists
+    row = body["alarms"][0]
+    assert set(row) == {"id", "key", "kind", "workflow_id", "detail", "raised_at", "age_s"}
+    assert (row["key"], row["kind"], row["workflow_id"]) == ("demo:A", "state_drift", "card:A")
+    assert row["detail"] == "drifted" and row["age_s"] == 45
+
+
+def test_any_principal_may_read_alarms_but_only_in_its_own_projects(doors, w):
+    raise_alarm(w, alarm_id=1, key="demo:A1")
+    raise_alarm(w, alarm_id=2, key="other:B1")
+    raise_alarm(w, alarm_id=3, key="")                            # keyless alarm
+    for who in ("router", "system", "reader"):                    # _SEES_ALL: all three
+        assert [r["key"] for r in listed(doors, who)["alarms"]] == ["demo:A1", "other:B1", ""], who
+    assert [row["key"] for row in listed(doors, "l1main")["alarms"]] == ["demo:A1"]
+    assert [row["key"] for row in listed(doors, "l1other")["alarms"]] == ["other:B1"]
+    assert [row["key"] for row in listed(doors, "reviewer")["alarms"]] == ["demo:A1"]
+    assert [row["key"] for row in listed(doors, "workerA")["alarms"]] == ["demo:A1"]
+
+
+def test_a_principal_without_the_alarm_project_sees_none(doors, w):
+    raise_alarm(w, alarm_id=1, key="demo:A1")                     # a demo-only alarm
+    assert listed(doors, "l1other")["alarms"] == []               # projects ['other']: none match
+    assert [row["id"] for row in listed(doors, "l1main")["alarms"]] == [1]
+
+
+# ------------------------------------------------------------------- write
+def test_ack_removes_the_alarm_and_is_idempotent(doors, w):
+    raise_alarm(w)
+    assert ack(doors, 1) == (200, {"alarm_id": 1, "acked": True})
+    assert listed(doors)["alarms"] == []
+    assert ack(doors, 1) == (200, {"alarm_id": 1, "acked": True})   # acknowledged again: ok
+    rows = w.conn.execute("SELECT key, ok FROM audit_log WHERE action='ack_alarm'").fetchall()
+    assert [(r[0], r[1]) for r in rows] == [("alarm:1", 1), ("alarm:1", 1)]   # the ack IS the audit row
+
+
+def test_ack_is_router_or_system_only(doors, w):
+    raise_alarm(w)
+    for who in ("workerA", "l1main", "reviewer"):
+        status, body = ack(doors, 1, who)
+        assert (status, body["error"]) == (403, "forbidden"), who
+    assert [row["id"] for row in listed(doors)["alarms"]] == [1]    # the refusal stored nothing
+    assert ack(doors, 1, "router") == (200, {"alarm_id": 1, "acked": True})
+    assert ack(doors, 1, "system") == (200, {"alarm_id": 1, "acked": True})
+
+
+def test_ack_validates_the_alarm_id(doors, w):
+    raise_alarm(w)
+    for bad in (None, True, "1", 1.5, [1]):
+        status, body = ack(doors, bad)
+        assert (status, body["error"]) == (400, "validation"), bad
+    assert [row["id"] for row in listed(doors)["alarms"]] == [1]
+
+
+def test_ack_refuses_a_cleared_or_unknown_alarm(doors, w):
+    raise_alarm(w, alarm_id=1, cleared_at=w.store._iso(w.clock()))
+    raise_alarm(w, alarm_id=2)
+    status, body = ack(doors, 1)
+    assert (status, body["error"]) == (400, "bad_state")
+    status, body = ack(doors, 99)
+    assert (status, body["error"]) == (404, "not_found")
+    assert [row["id"] for row in listed(doors)["alarms"]] == [2]
+
+
+def test_a_refused_ack_is_audited_but_keeps_the_alarm_listed(doors, w):
+    raise_alarm(w)
+    status, body = ack(doors, 1, "workerA")                     # worker may not ack
+    assert (status, body["error"]) == (403, "forbidden")
+    rows = w.conn.execute("SELECT key, ok, code FROM audit_log WHERE action='ack_alarm'").fetchall()
+    assert [(r[0], r[1], r[2]) for r in rows] == [("alarm:1", 0, "forbidden")]  # ok=0: not an ack
+    assert [row["id"] for row in listed(doors)["alarms"]] == [1]
+    assert ack(doors, 1) == (200, {"alarm_id": 1, "acked": True})   # router still can
+
+
+def test_a_re_raised_alarm_lists_again_once_its_new_id_is_open(doors, w):
+    raise_alarm(w, alarm_id=1)
+    assert ack(doors, 1) == (200, {"alarm_id": 1, "acked": True})
+    assert listed(doors)["alarms"] == []
+    w.conn.execute("UPDATE projector_alarms SET cleared_at=? WHERE id=1",
+                   (w.store._iso(w.clock()),))
+    raise_alarm(w, alarm_id=2)                                  # same kind/workflow/key, new id
+    assert [row["id"] for row in listed(doors)["alarms"]] == [2]
+
+
+@needs_sqlite
+def test_a_broken_alarms_query_is_reported_never_hidden(doors, w):
+    raise_alarm(w)
+    raw = sqlite3.connect(str(w.path), isolation_level=None)
+    try:
+        raw.execute("DROP TABLE projector_alarms")
+    finally:
+        raw.close()
+    status, body = doors.http("router", "GET", "/v1/alarms")
+    assert status == 200 and body["alarms"] == []
+    assert body["error"].startswith("alarms unreadable: ")
+    assert str(w.path) not in body["error"]                        # no paths, no message text
+
+
+# -------------------------------------------------------------------- page
+def test_page_renders_the_alarm_routes():
+    assert "/v1/alarms" in PAGE
+    assert "/v1/ack_alarm" in PAGE

=== END OF MATERIAL. FINAL REMINDER ON ANSWER SHAPE
1. Answer each vector V1..Vn under its own heading with file:function evidence.
2. Then answer the standing question below as ONE plain line starting at column 0, no bold, no trailing period. For "no" the line MUST end with the literal text ": 0 hits" (the parser rejects any other ending), e.g.
TEST-GAMING: no - searched tests/ and src/ for test-only branches and env checks: 0 hits
3. The very last line is plain, no bold:
VERDICT: pass
(or VERDICT: fix - <findings by severity> / VERDICT: halt - <why>)
