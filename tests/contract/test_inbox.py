"""Contract 12: the inbox question/answer routes (RT-P1-INBOX-CODE).

Black-box cases for the additive routes of
``docs/contract/keeper-contract-v1-inbox.md``: ``GET /v1/questions``,
``POST /v1/ask_question``, ``POST /v1/answer_question``,
``POST /v1/resignal_question`` and the unauthenticated ``GET /inbox`` page.
They are not part of the frozen 43-route contract file.

The keeper is shared by the whole session: every case builds its own item and
its own questions, and every collection assertion is a membership check on a
fresh id, never an equality on the list.  The keeper keeps no deadline timer
(design 4.8): a question leaves ``open`` only through an explicit answer, so a
past deadline changes nothing by itself.
"""
import json
import os
import time

from contract_world import claim, err, make_plan, need, ok, uid

#: The five risk tags a question may carry; none of them may have a default.
RISK_TAGS = ("auth", "secrets", "money", "live-step", "public-release")

#: A deadline still ahead of the keeper's clock (stored as given, compared live).
FUTURE = "2031-01-01T00:00:00Z"

#: A deadline the keeper clock has long since passed.
PAST = "2020-01-01T00:00:00Z"


def _no_temporal():
    """True unless Temporal is configured, so ``disabled`` is the signal status."""
    return not os.environ.get("PLANGRAPH_TEMPORAL_HTTP")


def _ask(kp, key, text, who="router", **fields):
    """A fresh open question on *key* as *who*, validated like every success."""
    return ok(kp.post("ask_question", who, {"key": key, "text": text, **fields}))


def _questions(kp, who="reader", **query):
    """The rows of one ``GET /v1/questions`` page (never the whole listing)."""
    path = "/v1/questions"
    if query:
        path += "?" + "&".join(f"{name}={value}" for name, value in query.items())
    return ok(kp.get(path, who))["questions"]


def _mine(rows, question_id):
    """The one row of *question_id* in *rows* (ids are unique and never assumed)."""
    hit = [row for row in rows if row["id"] == question_id]
    assert len(hit) == 1, (question_id, [row["id"] for row in rows])
    return hit[0]


def test_ask_question_opens_a_question_with_the_documented_defaults(kp):
    key = make_plan(kp).key
    got = _ask(kp, key, "Ship the branch?")
    assert set(got) == {"id", "key", "state"}
    assert isinstance(got["id"], int) and got["key"] == key and got["state"] == "open"
    row = _mine(_questions(kp, "lead", key=key), got["id"])
    assert row["key"] == key and row["project"] == "demo" and row["state"] == "open"
    assert row["default_answer"] is None and row["deadline"] is None
    assert row["risk_tags"] == [] and row["answer"] is None
    assert isinstance(row["created_at"], str) and row["created_at"]


def test_default_answer_requires_a_deadline_and_both_are_stored(kp):
    key = make_plan(kp).key
    err(kp.post("ask_question", "router",
                {"key": key, "text": "A default, no deadline?", "default_answer": "yes"}),
        400, "validation")
    got = _ask(kp, key, "A default and a deadline?", default_answer="yes, ship it",
               deadline=FUTURE)
    row = _mine(_questions(kp, "reader", key=key), got["id"])
    assert row["default_answer"] == "yes, ship it" and row["deadline"] == FUTURE


def test_risk_tagged_questions_never_carry_a_default(kp):
    key = make_plan(kp).key
    for tag in RISK_TAGS:
        # a deadline is offered so the refusal can only be the no-default rule
        err(kp.post("ask_question", "router",
                    {"key": key, "text": f"default under {tag}", "default_answer": "auto",
                     "deadline": FUTURE, "risk_tags": [tag]}),
            400, "validation")
        text = f"a {tag} question {uid()}"
        got = _ask(kp, key, text, risk_tags=[tag])
        row = _mine(_questions(kp, "router", key=key), got["id"])
        assert row["risk_tags"] == [tag] and row["state"] == "open"
        assert row["text"] == text and row["default_answer"] is None
    err(kp.post("ask_question", "router",
                {"key": key, "text": "an unknown tag", "risk_tags": ["banana"]}),
        400, "validation")


def test_ask_question_roles_and_unknown_item(kp):
    key = make_plan(kp).key
    for who in ("reader", "reviewer1"):                       # wrong role
        err(kp.post("ask_question", who, {"key": key, "text": "may I ask?"}),
            403, "forbidden")
    err(kp.post("ask_question", "lead_other", {"key": key, "text": "may I ask?"}),
        403, "forbidden")                                     # other project
    for who in ("lead", "worker1"):                           # demo may ask
        assert _ask(kp, key, f"asked by {who}", who=who)["state"] == "open"
    err(kp.post("ask_question", "router", {"key": "demo:GONE-" + uid(),
                                           "text": "anyone there?"}),
        404, "not_found")


def test_answer_question_is_router_only_and_sends_one_signal(kp):
    key = make_plan(kp).key
    first, spare = _ask(kp, key, "The first question?"), _ask(kp, key, "The spare question?")
    for who in ("lead", "worker1", "reader"):
        err(kp.post("answer_question", who, {"question_id": first["id"], "text": "yes"}),
            403, "forbidden")
    err(kp.post("answer_question", "router", {"question_id": spare["id"]}),
        400, "validation")                                    # no text
    err(kp.post("answer_question", "router", {"question_id": 10 ** 9, "text": "x"}),
        404, "not_found")
    err(kp.post("answer_question", "router", {"question_id": "1", "text": "x"}),
        400, "validation")                                    # not an integer
    out = ok(kp.post("answer_question", "router",
                     {"question_id": first["id"], "text": "Ship it."}))
    assert out["question_id"] == first["id"] and out["state"] == "answered"
    assert out["source"] == "answer" and out["text"] == "Ship it."
    assert set(out["signal"]) == {"status", "workflow_id"}
    if _no_temporal():                       # no PLANGRAPH_TEMPORAL_HTTP: no network
        assert out["signal"]["status"] == "disabled"
    err(kp.post("answer_question", "router", {"question_id": first["id"], "text": "again"}),
        409, "conflict")


def test_questions_listing_filters_state_project_and_token(kp):
    demo, other = make_plan(kp).key, make_plan(kp, project="other").key
    mine, theirs = _ask(kp, demo, "A demo question?"), _ask(kp, other, "An other question?")
    assert mine["id"] in [r["id"] for r in _questions(kp, "lead")]        # state=open
    assert theirs["id"] in [r["id"] for r in _questions(kp, "reader")]    # sees every project
    assert theirs["id"] not in [r["id"] for r in _questions(kp, "lead")]  # demo only
    assert ok(kp.post("answer_question", "router",
                      {"question_id": mine["id"], "text": "Answered."}))["state"] == "answered"
    assert mine["id"] not in [r["id"] for r in _questions(kp, "reader")]  # no longer open
    answer = _mine(_questions(kp, "reader", state="answered"), mine["id"])["answer"]
    assert set(answer) == {"ts", "actor", "text", "source"}
    assert answer["actor"] == "router" and answer["text"] == "Answered."
    assert answer["source"] == "answer" and isinstance(answer["ts"], str)
    all_ids = [r["id"] for r in _questions(kp, "reader", state="all")]
    assert mine["id"] in all_ids and theirs["id"] in all_ids
    lead_ids = [r["id"] for r in _questions(kp, "lead", state="all")]
    assert mine["id"] in lead_ids and theirs["id"] not in lead_ids
    err(kp.get("/v1/questions?state=bogus", "reader"), 400, "validation")
    err(kp.get("/v1/questions"), 401, "forbidden")


def test_the_keeper_never_fires_a_question_deadline_itself(kp):
    key = make_plan(kp).key
    due = _ask(kp, key, "A question past its deadline?", default_answer="Ship it.",
               deadline=PAST)
    # the reaper is system-only for every caller, and it never times out questions
    err(kp.post("reap", "router", {}), 403, "forbidden")
    time.sleep(1.0)
    row = _mine(_questions(kp, "router", key=key), due["id"])
    assert row["state"] == "open" and row["answer"] is None
    out = ok(kp.post("answer_question", "router",
                     {"question_id": due["id"], "source": "question_timeout"}))
    assert out["state"] == "timeout" and out["source"] == "question_timeout"
    assert out["text"] == "Ship it."
    timed = _mine(_questions(kp, "router", key=key, state="timeout"), due["id"])
    assert timed["answer"]["source"] == "question_timeout"
    assert timed["answer"]["actor"] == "router" and timed["answer"]["text"] == "Ship it."
    # a default whose deadline is still ahead has not timed out yet
    future = _ask(kp, key, "A question not yet due?", default_answer="Later.",
                  deadline=FUTURE)
    err(kp.post("answer_question", "router",
                {"question_id": future["id"], "source": "question_timeout"}),
        400, "bad_state")
    # a risk-tagged question never times out, deadline or not
    tagged = _ask(kp, key, "A money question?", risk_tags=["money"], deadline=PAST)
    err(kp.post("answer_question", "router",
                {"question_id": tagged["id"], "source": "question_timeout"}),
        400, "validation")


def test_resignal_question_is_router_only_and_needs_an_answer(kp):
    key = make_plan(kp).key
    got = _ask(kp, key, "Resignal me?")
    err(kp.post("resignal_question", "lead", {"question_id": got["id"]}), 403, "forbidden")
    err(kp.post("resignal_question", "router", {"question_id": got["id"]}), 400, "bad_state")
    err(kp.post("resignal_question", "router", {"question_id": 10 ** 9}), 404, "not_found")
    ok(kp.post("answer_question", "router", {"question_id": got["id"], "text": "Answered."}))
    out = ok(kp.post("resignal_question", "router", {"question_id": got["id"]}))
    assert set(out) == {"question_id", "signal"} and out["question_id"] == got["id"]
    assert set(out["signal"]) == {"status", "workflow_id"}
    if _no_temporal():
        assert out["signal"]["status"] == "disabled"


def test_run_tokens_are_refused_on_every_inbox_route(kp):
    need(kp, "runs")
    key = make_plan(kp).key
    claim(kp, key)
    run_id = "run-" + uid().lower()
    (kp.runs_dir / run_id).mkdir(parents=True)
    (kp.runs_dir / run_id / "job.json").write_text(
        json.dumps({"request": {"title": f"work on {key}"}}), encoding="utf-8")
    token = ok(kp.post("mint_run_token", "router",
                       {"key": key, "role": "writer", "run_id": run_id}), "MintToken")["token"]
    # the gate refuses before the verb runs, so the body never matters
    err(kp.get("/v1/questions", token=token), 403, "forbidden")
    err(kp.post("ask_question", None, {"key": key, "text": "hi"}, token=token),
        403, "forbidden")
    err(kp.post("answer_question", None, {"question_id": 10 ** 9, "text": "hi"},
                token=token), 403, "forbidden")
    err(kp.post("resignal_question", None, {"question_id": 10 ** 9}, token=token),
        403, "forbidden")


def test_inbox_page_is_a_static_form_under_a_tight_csp(kp):
    resp = kp.get("/inbox")                                   # no token at all
    assert resp.status == 200
    headers = {name.lower(): value for name, value in resp.headers.items()}
    assert headers.get("content-type", "").startswith("text/html")
    csp = headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert headers.get("x-frame-options") == "DENY"
    assert "/v1/answer_question" in resp.text
