"""PG-ANSWER-UI: the operator verb ``operator_answer`` (open operator items), event ``answered``.

Mutation killed by these tests: relaxing the role in ``_operator_answer`` to
``router`` fails ``test_every_other_role_is_forbidden``; dropping the
``item["operator"]`` check fails ``test_item_not_marked_operator_is_refused``;
matching a choice id by prefix instead of equality fails
``test_wrong_choice_id_is_refused``.
"""
import pytest

from keeper_util import World, defn, to_claimed
from plangraph.keeper import auth, schema
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import read_events, verify_integrity

CAP = 1000
USER = "ops-admin"
CHOICES = {"options": [{"id": "ship", "label": "Ship it", "consequence": "go now"},
                       {"id": "hold", "label": "Hold off"}],
           "default": "hold", "deadline": None, "blocks": None}


class FakeRuns:
    """Just enough of the run record to mint a writer token; never touches disk."""

    def valid_run_id(self, run_id):
        return run_id == "run-1"

    def job(self, run_id):
        if run_id != "run-1":
            raise FileNotFoundError(run_id)
        return {"request": {"title": "work on demo:L"}, "task": ""}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("Q", kind="question", operator=True, wait="answer", choices=CHOICES),
        defn("Q2", kind="question", operator=True, wait="answer"),
        defn("D", kind="decision", operator=True, wait="answer"),
        defn("T", kind="operator_task", operator=True, wait="task"),
        defn("PLAIN", kind="question", operator=False, wait="answer",
             choices=CHOICES),
        defn("L"), defn("G", ["Q", "Q2", "D", "T", "PLAIN", "L"], kind="goal")])
    world.store.ctx = {"runs": FakeRuns()}
    for name, role in (("operator", "operator"), ("spawner1", "spawner")):
        token = auth.add_principal(world.conn, name, role, ["demo"])
        world.tokens[name] = token
        world.principals[name] = auth.authenticate(world.conn, token)
    yield world
    world.close()


def _item(w, key):
    return w.store._item(key)


def _answer(w, key="demo:Q", **kw):
    return w.store.operator_answer(w.p("operator"), key, user=USER, **kw)


def _answered(w, key="demo:Q"):
    events = [e for e in read_events(w.conn)
              if e["type"] == "answered" and e["key"] == key]
    assert len(events) == 1
    return events[0]


def _code(fn, *a, **kw):
    with pytest.raises(Refused) as exc:
        fn(*a, **kw)
    return exc.value.code


def test_operator_role_is_known():
    assert "operator" in schema.ROLES


def test_answer_open_question_by_choice_id(w):
    out = _answer(w, choice_id="ship")
    assert out == {"key": "demo:Q", "state": "done", "choice_id": "ship",
                   "source": "ui"}
    item = _item(w, "demo:Q")
    assert item["state"] == "done" and item["reviewer"] == "operator"
    event = _answered(w)
    assert event["payload"] == {"text": "Ship it", "choice_id": "ship",
                                "source": "ui", "user": USER, "decision_id": None}
    assert verify_integrity(w.conn) == []


def test_answer_open_question_by_free_text(w):
    out = _answer(w, "demo:Q2", text="ship it, once the backup lands")
    assert out == {"key": "demo:Q2", "state": "done", "choice_id": None,
                   "source": "ui"}
    assert _answered(w, "demo:Q2")["payload"]["text"] == "ship it, once the backup lands"
    assert verify_integrity(w.conn) == []


@pytest.mark.parametrize("key,choice", [("demo:D", "hold"), ("demo:T", "ship")])
def test_answer_the_other_operator_kinds(w, key, choice):
    w.store.edit_definition(w.p("router"), key, {"choices": CHOICES})
    assert _answer(w, key, choice_id=choice)["state"] == "done"
    assert _item(w, key)["state"] == "done"
    assert verify_integrity(w.conn) == []


def test_text_cap_boundary(w):
    assert _answer(w, "demo:Q2", text="x" * CAP)["state"] == "done"
    assert _code(_answer, w, text="x" * (CAP + 1)) == "validation"


def test_credential_looking_text_is_refused(w):
    secret = "log" + "in admin/" + "hunter2" + "secret"
    assert _code(_answer, w, text=secret) == "validation"
    assert _item(w, "demo:Q")["state"] == "open"


@pytest.mark.parametrize("bad", ["nope", "SHIP", "ship ", "", 5, ["ship"]])
def test_wrong_choice_id_is_refused(w, bad):
    assert _code(_answer, w, choice_id=bad) == "validation"
    assert _item(w, "demo:Q")["state"] == "open"


def test_choice_id_needs_choices_on_the_item(w):
    assert _code(_answer, w, "demo:Q2", choice_id="ship") == "validation"


@pytest.mark.parametrize("kw", [{"choice_id": "ship", "text": "both"},
                                {"user": USER}])
def test_exactly_one_of_choice_id_or_text(w, kw):
    kw.setdefault("user", USER)
    assert _code(w.store.operator_answer, w.p("operator"), "demo:Q", **kw) \
        == "validation"
    assert _item(w, "demo:Q")["state"] == "open"


@pytest.mark.parametrize("user", [None, "", 5, "a" * 65, "has space", "no~tilde"])
def test_bad_user_is_refused(w, user):
    assert _code(w.store.operator_answer, w.p("operator"), "demo:Q",
                 choice_id="ship", user=user) == "validation"
    assert _item(w, "demo:Q")["state"] == "open"


def test_claimed_item_is_refused(w):
    to_claimed(w, "demo:Q")
    assert _code(_answer, w, choice_id="ship") == "bad_state"
    assert _item(w, "demo:Q")["state"] == "claimed"


@pytest.mark.parametrize("text", ["", "   ", 5, ["x"]])
def test_bad_text_is_refused(w, text):
    assert _code(_answer, w, text=text) == "validation"
    assert _item(w, "demo:Q")["state"] == "open"


def test_item_not_marked_operator_is_refused(w):
    assert _code(_answer, w, "demo:PLAIN", choice_id="ship") == "validation"
    assert _item(w, "demo:PLAIN")["state"] == "open"


def test_lane_kind_is_refused(w):
    assert _code(_answer, w, "demo:L", text="go") == "validation"
    assert _item(w, "demo:L")["state"] == "open"


@pytest.mark.parametrize("who", ["router", "l1main", "workerA", "reviewer", "reader",
                                 "spawner1", "system"])
def test_every_other_role_is_forbidden(w, who):
    assert _code(w.store.operator_answer, w.p(who), "demo:Q",
                 choice_id="ship", user=USER) == "forbidden"
    assert _item(w, "demo:Q")["state"] == "open"


def test_run_token_is_forbidden(w):
    to_claimed(w, "demo:L")
    minted = w.store.mint_run_token(w.p("l1main"), "demo:L", "writer", "run-1")
    token_principal = auth.authenticate(w.conn, minted["token"])
    assert isinstance(token_principal, auth.RunPrincipal)
    assert _code(w.store.operator_answer, token_principal, "demo:Q",
                 choice_id="ship", user=USER) == "forbidden"
    assert _item(w, "demo:Q")["state"] == "open"


def test_operator_may_not_call_the_router_verb(w):
    assert _code(w.store.answer, w.p("operator"), "demo:Q", "x", "D-500") \
        == "forbidden"
    assert _item(w, "demo:Q")["state"] == "open"


def test_done_item_is_locked(w):
    _answer(w, choice_id="ship")
    assert _code(_answer, w, choice_id="hold") == "locked"
    assert _answered(w)["payload"]["choice_id"] == "ship"


def test_answer_map_shows_the_ui_answer(w):
    _answer(w, choice_id="ship")
    hit = w.store.answer_map()["demo:Q"]
    assert hit["text"] == "Ship it" and hit["decision_id"] is None
    assert hit["ts"] == _answered(w)["ts"]
