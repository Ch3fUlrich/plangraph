"""KEEPER-OPTASK-CLOSE (D-2201/D-2206): an operator task closes on a decision.

An ``operator_task`` item is work the operator does by hand, so it is never
reviewed by an agent.  The router may close it straight from ``claimed`` /
``in_review`` to ``done`` through ``close_on_decision`` at ANY review tier
(``standard`` or ``high``), provided the decision evidence verifies and the
decision row names the key in a whole ``Closes: <project:ID>`` token.  Unlike a
``review_tier='none'`` item, a non-``none`` tier is NOT authorised by the hashed
batch file alone: a decision without a ``Closes`` token must not close it.

Everything else is unchanged: only the router may call the verb, a decision that
names another key is refused ``evidence_unverified``, every other kind (lane,
question, goal, ...) at a non-``none`` tier is still ``bad_state``, and the
tier/kind checks run again under the write lock so a row that changes between
the pre-lock verification and the transaction is a ``conflict``.

SQLite and Postgres both run these tests: ``keeper_util.World`` follows
``PG_KEEPER_BACKEND``.
"""
from types import SimpleNamespace

import pytest

from keeper_util import World, defn, to_claimed, to_in_review
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from test_keeper_apply_ops import create, link_goal, snap
from test_keeper_close import (CLOSES, CLOSES_OTHER, DID, KEY, NO_CLOSES,
                               _batch, _decisions)

OTHER = "demo:Z"


def _seed(w, *, kind="operator_task", tier="high"):
    """One item of *kind* at *tier*, plus a goal that depends on it.

    ``operator_task`` is a normal plan item at create time (``item_fields``
    gives it ``operator=True``/``wait='task'``); the goal is needed because
    ``views.validate`` refuses work that cannot reach one.
    """
    items = [defn("A", [], kind=kind, evidence_kinds=["decision"],
                  review_tier=tier)]
    if kind != "goal":
        items.append(defn("GOAL", ["A"], kind="goal"))
    w.store.create_items(w.p("router"), items)


def _world(tmp_path, body, *, kind="operator_task", tier="high",
           state="claimed"):
    """A world with ``demo:A`` claimed/in_review and a decision row attached.

    ``body`` is one decisions-file row; ``CLOSES``/``NO_CLOSES``/``CLOSES_OTHER``
    come from the existing close tests.  ``w.batch_ref`` is a hashed file under
    ``tmp_path/plan``, so the batch-file half of the proof is always satisfied.
    """
    w = World(tmp_path)
    _seed(w, kind=kind, tier=tier)
    path = _decisions(tmp_path, body)
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(path))
    _, ref, _ = _batch(tmp_path)
    w.batch_ref = ref
    w.store.batch_roots = [str(tmp_path / "plan")]
    if state == "claimed":
        to_claimed(w, KEY)
        w.store.add_evidence_router(w.p("router"), KEY, "decision",
                                    {"id": DID})
    else:
        to_in_review(w, KEY, ref={"id": DID})
    return w


def _refuse(w, code, principal="router", key=KEY):
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p(principal), key, w.batch_ref)
    assert exc.value.code == code, f"{exc.value.code} != {code}: {exc.value.msg}"
    return exc.value


# ------------------------------------------------ 1. high tier: close on decision
def test_operator_task_high_closes_from_claimed(tmp_path):
    w = _world(tmp_path, CLOSES)
    # The fixture really is a claimed high-tier operator task: today the verb
    # refuses it with ``bad_state`` ("only 'none' closes on a decision").
    item = w.store._item(KEY)
    assert (item["kind"], item["review_tier"], item["state"]) \
        == ("operator_task", "high", "claimed")

    out = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)

    assert out["state"] == "done"
    assert out["decision_ids"] == [DID]
    assert w.store._item(KEY)["state"] == "done"
    row = w.conn.execute(
        "SELECT actor FROM events WHERE key=? AND type='closed_on_decision'",
        (KEY,)).fetchone()
    assert row is not None and row["actor"] == "router"
    w.close()


def test_closed_operator_task_is_locked(tmp_path):
    w = _world(tmp_path, CLOSES)
    w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), KEY, {"title": "later"})
    assert exc.value.code == "locked"
    w.close()


# -------------------------------------- 2. standard tier closes from in_review
@pytest.mark.parametrize("tier", ["standard", "high"])
def test_operator_task_closes_from_in_review(tmp_path, tier):
    w = _world(tmp_path, CLOSES, tier=tier, state="in_review")
    assert w.store._item(KEY)["state"] == "in_review"

    out = w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)

    assert out["state"] == "done"
    assert w.store._item(KEY)["state"] == "done"
    w.close()


# ----------------------------------- 3. no Closes token is not enough above none
def test_no_closes_token_does_not_close_a_high_operator_task(tmp_path):
    """A tier above ``none`` needs the decision to NAME the key.

    The spec allows either code here (``evidence_unverified`` is the intended
    one: the batch file alone must not authorise a reviewed tier; a
    ``bad_state``-first refusal is also acceptable).  Pin the implementation's
    real code in the comment once it lands.
    """
    w = _world(tmp_path, NO_CLOSES)
    item = w.store._item(KEY)
    assert (item["kind"], item["review_tier"], item["state"]) \
        == ("operator_task", "high", "claimed")

    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)

    assert exc.value.code in ("evidence_unverified", "bad_state"), exc.value.code
    assert w.store._item(KEY)["state"] != "done"
    w.close()


# --------------------------------- 4. a decision naming another key is refused
def test_decision_naming_another_key_is_refused(tmp_path):
    w = _world(tmp_path, CLOSES_OTHER)
    exc = _refuse(w, "evidence_unverified")
    assert OTHER in exc.msg
    assert w.store._item(KEY)["state"] == "claimed"
    w.close()


# ---------------------------------------- 5. the router is still the only caller
@pytest.mark.parametrize("who", ["l1main", "reviewer", "workerA"])
def test_non_router_cannot_close_an_operator_task(tmp_path, who):
    w = _world(tmp_path, CLOSES)
    _refuse(w, "forbidden", principal=who)
    assert w.store._item(KEY)["state"] == "claimed"
    w.close()


# ------------------------------------------ 6. every other kind is still refused
@pytest.mark.parametrize("kind", ["lane", "question", "goal"])
def test_other_kinds_above_none_still_refused(tmp_path, kind):
    """Only ``operator_task`` gained the wider close; a valid ``Closes`` token
    changes nothing for lane/question/goal."""
    w = _world(tmp_path, CLOSES, kind=kind)
    assert w.store._item(KEY)["kind"] == kind
    assert w.store._item(KEY)["review_tier"] == "high"

    exc = _refuse(w, "bad_state")

    assert "review_tier" in exc.msg
    assert w.store._item(KEY)["state"] == "claimed"
    w.close()


# --------------------------- 8. a kind/tier change between pre-lock and lock
def _flip_between_prelock_and_lock(w, monkeypatch, column, value):
    """Change ``items.<column>`` on the verb's first ``_read_batch`` call.

    ``_read_batch`` is the last slow pre-lock step, so the flip lands after the
    pre-lock tier/kind check and before ``BEGIN IMMEDIATE`` -- exactly the race
    the in-lock re-check exists for.  The hook flips the row only once.
    """
    original = w.store._read_batch
    flipped = []

    def hook(batch_ref):
        result = original(batch_ref)
        if not flipped:
            flipped.append(True)
            w.conn.execute(f"UPDATE items SET {column}=? WHERE key=?",
                           (value, KEY))
        return result

    monkeypatch.setattr(w.store, "_read_batch", hook)
    return flipped


@pytest.mark.parametrize("column,value", [("review_tier", "standard"),
                                         ("kind", "lane")])
def test_kind_or_tier_change_under_the_transaction_is_a_conflict(
        tmp_path, monkeypatch, column, value):
    w = _world(tmp_path, CLOSES)
    flipped = _flip_between_prelock_and_lock(w, monkeypatch, column, value)

    exc = _refuse(w, "conflict")

    assert flipped, "the pre-lock hook never ran"
    assert column in exc.msg or "changed" in exc.msg
    assert w.store._item(KEY)["state"] != "done"
    w.close()


# ---------------------------------------- 9. the same close inside apply_ops
def test_operator_task_close_inside_apply_ops(tmp_path):
    w = _world(tmp_path, CLOSES)

    out = w.store.apply_ops(w.p("router"),
                            [{"op": "close_on_decision", "key": KEY}],
                            batch_ref=w.batch_ref)

    assert out["applied"] == 1
    assert w.store._item(KEY)["state"] == "done"
    w.close()


def test_other_kind_refusal_rolls_back_the_whole_batch(tmp_path):
    """Mirror ``test_fail_closed_also_inside_apply_ops``: the refusal of a
    non-operator_task high item takes the batch's create/link down with it."""
    w = _world(tmp_path, CLOSES, kind="lane")
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [
            create("X"), link_goal("A", "X"),
            {"op": "close_on_decision", "key": KEY}], batch_ref=w.batch_ref)

    assert exc.value.code in ("bad_state", "conflict"), exc.value.code
    assert exc.value.msg.startswith("op 3 close_on_decision demo:A: ")
    assert snap(w) == before
    assert w.store._item("demo:X") is None
    assert w.store._item(KEY)["state"] == "claimed"
    w.close()
