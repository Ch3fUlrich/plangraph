"""D-434 (widened by F4): the router's narrow definition edits on an active item.

``edit_definition`` freezes a definition once the item leaves ``open``.  The
single exception is the router's (decision D-434, widened by F4): it alone may
change ``review_tier``, ``evidence_kinds``, ``done_criteria`` and
``depends_on`` -- and nothing else -- while the item is ``claimed`` or
``in_review`` and not yet locked.  Every such edit bumps ``round``, so the
verdicts the earlier round recorded stop counting, and carries a
``"while": "<state>"`` marker the fold replays the bump from, so
``verify_integrity`` agrees with the cache.  Each edit's ``item_defined`` event
also carries ``who`` plus the ``before``/``after`` values of every changed
field (F4 audit).

The same shape must survive ``apply_ops``: one transaction re-tiering to
``none``, attaching the decision and closing on it -- or rolling all of it
back when the last op fails.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from keeper_util import World, seed, to_claimed, to_done, to_in_review
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused

KEY = "demo:A"
DID = "D-434"


class FakeDecisions:
    """A decisions-adapter double: known ids close every key it is handed."""

    def __init__(self, ids, closes):
        self.ids = set(ids)
        self.closed = set(closes)

    def has(self, decision_id):
        return decision_id in self.ids

    def closes(self, decision_id):
        return set(self.closed) if decision_id in self.ids else set()


def last_defined(w, key=KEY):
    """The newest ``item_defined`` event (the attributed edit) for *key*."""
    hits = [e for e in keeper_events.read_events(w.conn)
            if e["key"] == key and e["type"] == "item_defined"]
    assert hits, "the edit wrote no item_defined event"
    return hits[-1]


def snap(w):
    """Row counts plus the hash-chain head: byte-for-byte unchanged proof."""
    q = w.conn.execute
    head = q("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return (q("SELECT COUNT(*) FROM items").fetchone()[0],
            q("SELECT COUNT(*) FROM edges").fetchone()[0],
            q("SELECT COUNT(*) FROM events").fetchone()[0],
            q("SELECT COUNT(*) FROM evidence").fetchone()[0],
            (head["seq"], head["hash"]))


def folded(w, key=KEY):
    return keeper_events.fold(keeper_events.read_events(w.conn))[key]


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


# ------------------------------------------------- the exception, one field
def test_router_re_tiers_claimed_item_with_while_marker(w):
    epoch = to_claimed(w, KEY)
    out = w.store.edit_definition(w.p("router"), KEY,
                                  {"review_tier": "none",
                                   "evidence_kinds": ["decision"]})
    assert out == {"key": KEY}
    item = w.store._item(KEY)
    assert (item["state"], item["epoch"]) == ("claimed", epoch)
    assert item["review_tier"] == "none"
    assert item["evidence_kinds"] == ["decision"]
    assert item["round"] == 1          # claimed rounds start at 0
    event = last_defined(w)
    assert (event["actor"], event["role"]) == ("router", "router")
    assert event["payload"] == {"fields": ["evidence_kinds", "review_tier"],
                                "review_tier": "none", "while": "claimed",
                                "who": "router",
                                "before": {"evidence_kinds": ["any"],
                                           "review_tier": "standard"},
                                "after": {"evidence_kinds": ["decision"],
                                          "review_tier": "none"}}
    cache = w.store._item(KEY)
    assert folded(w)["round"] == cache["round"]
    assert folded(w)["review_tier"] == "none"
    assert w.store.verify_integrity() == []
    assert w.store.verify_chain() == []


def test_router_edits_evidence_kinds_on_in_review_item(w):
    epoch = to_in_review(w, KEY)               # submit bumped round to 1
    out = w.store.edit_definition(w.p("router"), KEY,
                                  {"evidence_kinds": ["decision"]})
    assert out == {"key": KEY}
    item = w.store._item(KEY)
    assert (item["state"], item["epoch"]) == ("in_review", epoch)
    assert item["review_tier"] == "standard"   # untouched, lower not implied
    assert item["evidence_kinds"] == ["decision"]
    assert item["round"] == 2
    event = last_defined(w)
    assert event["actor"] == "router"
    assert event["payload"] == {"fields": ["evidence_kinds"],
                                "while": "in_review", "who": "router",
                                "before": {"evidence_kinds": ["any"]},
                                "after": {"evidence_kinds": ["decision"]}}
    assert folded(w)["round"] == item["round"]
    assert w.store.verify_integrity() == []
    assert w.store.verify_chain() == []


def test_tier_none_still_needs_decision_only_evidence_while_claimed(w):
    """The tier rules ride along with the exception; they are not waived."""
    to_claimed(w, KEY)
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), KEY, {"review_tier": "none"})
    assert exc.value.code == "validation"
    assert "evidence_kinds" in exc.value.msg
    item = w.store._item(KEY)
    assert item["review_tier"] == "standard"
    assert item["round"] == 0
    assert snap(w) == before
    assert w.store.verify_integrity() == []


# ------------------------------------------------------------- voided verdicts
def test_router_edit_voids_the_current_rounds_verdicts(tmp_path):
    w = World(tmp_path)
    seed(w, [("A", [])], review_tier="high")
    to_in_review(w, KEY)                                   # round 1
    first = w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    assert first["state"] == "in_review"                   # high needs two
    assert len(w.store._round_verdicts(w.store._item(KEY))) == 1

    w.store.edit_definition(w.p("router"), KEY,
                            {"evidence_kinds": ["decision"]})
    item = w.store._item(KEY)
    assert item["review_tier"] == "high"
    assert item["round"] == 2
    assert w.store._round_verdicts(item) == []             # stale verdict voided
    stored = w.conn.execute("SELECT COUNT(*) FROM events WHERE key=?"
                            " AND type='review_verdict'", (KEY,)).fetchone()[0]
    assert stored == 1                                     # still recorded...
    assert folded(w)["round"] == 2                         # ...under the old round
    assert w.store.verify_integrity() == []

    # The voided seat must not count: one new verdict keeps the item in review
    # (it would close it only if the stale one still tallied), a second closes.
    second = w.store.verdict(w.p("reviewer2"), KEY, True, "ok")
    assert second["state"] == "in_review"
    third = w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    assert third["state"] == "done"
    assert w.store.verify_integrity() == []
    w.close()


# ------------------------------------------------------------------ refusals
def test_extra_field_in_the_same_edit_stays_refused(w):
    to_claimed(w, KEY)
    events_before = snap(w)[2]
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), KEY,
                                {"review_tier": "high", "title": "renamed"})
    assert exc.value.code == "bad_state"
    assert "not open" in exc.value.msg
    item = w.store._item(KEY)
    assert item["title"] == "title A"
    assert item["review_tier"] == "standard"
    assert item["round"] == 0
    assert snap(w)[2] == events_before
    assert w.store.verify_integrity() == []


def test_non_router_cannot_edit_tier_fields_while_claimed(w):
    to_claimed(w, KEY)
    events_before = snap(w)[2]
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("l1main"), KEY,
                                {"evidence_kinds": ["decision"]})
    assert exc.value.code == "forbidden"
    assert "only the router may edit" in exc.value.msg
    for who in ("workerA", "reviewer", "reader", "system"):
        with pytest.raises(Refused) as exc:
            w.store.edit_definition(w.p(who), KEY,
                                    {"review_tier": "high",
                                     "evidence_kinds": ["decision"]})
        assert exc.value.code == "forbidden", who
    item = w.store._item(KEY)
    assert item["state"] == "claimed"
    assert item["review_tier"] == "standard"
    assert item["evidence_kinds"] == ["any"]
    assert item["round"] == 0
    assert snap(w)[2] == events_before   # no item_defined from any of them
    assert w.store.verify_integrity() == []


@pytest.mark.parametrize("how", ["done", "cancelled"])
def test_locked_items_refuse_the_router_edit(tmp_path, how):
    w = World(tmp_path)
    seed(w, [("A", [])])
    if how == "done":
        to_done(w, KEY)
    else:
        to_claimed(w, KEY)
        w.store.cancel(w.p("router"), KEY, "abandoned")
    events_before = snap(w)[2]
    round_before = w.store._item(KEY)["round"]
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), KEY,
                                {"review_tier": "none",
                                 "evidence_kinds": ["decision"]})
    assert exc.value.code == "locked"
    assert (how if how == "cancelled" else "DONE") in exc.value.msg
    item = w.store._item(KEY)
    assert item["review_tier"] == "standard"
    assert item["round"] == round_before  # no bump for a refused edit
    assert snap(w)[2] == events_before
    assert w.store.verify_integrity() == []
    w.close()


# ------------------------------------------------------------- apply_ops batch
def close_ops():
    return [{"op": "edit", "key": KEY,
             "fields": {"review_tier": "none",
                        "evidence_kinds": ["decision"]}},
            {"op": "add_evidence", "key": KEY, "kind": "decision",
             "ref": {"id": DID}},
            {"op": "close_on_decision", "key": KEY}]


def batch_world(tmp_path):
    """A claimed, standard-tier item plus a closed-up decisions adapter and a
    hashed batch file inside a configured ``batch_roots`` directory."""
    w = World(tmp_path)
    seed(w, [("A", [])])
    w.store.ctx = SimpleNamespace(decisions=FakeDecisions([DID], [KEY]))
    plan = tmp_path / "plan"
    plan.mkdir()
    path = plan / "batch.md"
    path.write_text("batch body\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    w.batch_ref = f"{path}@{digest}"
    w.store.batch_roots = [str(plan)]
    to_claimed(w, KEY)
    return w


def test_batch_re_tiers_adds_evidence_and_closes_atomically(tmp_path):
    w = batch_world(tmp_path)
    assert w.store._item(KEY)["review_tier"] == "standard"
    before = snap(w)
    out = w.store.apply_ops(w.p("router"), close_ops(), batch_ref=w.batch_ref)
    assert out["applied"] == 3
    assert out["results"][0] == {"key": KEY}
    assert out["results"][1]["verified"] is True
    assert out["results"][2]["state"] == "done"
    item = w.store._item(KEY)
    assert item["state"] == "done"
    assert item["review_tier"] == "none"
    assert item["evidence_kinds"] == ["decision"]
    assert item["round"] == 1                  # the re-tier bumped it
    assert snap(w)[2] == before[2] + 3         # edit + evidence + close
    assert last_defined(w)["payload"]["while"] == "claimed"
    assert folded(w)["round"] == 1
    assert w.store.verify_integrity() == []
    assert w.store.verify_chain() == []
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), KEY, {"title": "later"})
    assert exc.value.code == "locked"
    w.close()


def test_batch_failing_last_op_rolls_back_the_re_tier_and_evidence(tmp_path):
    w = batch_world(tmp_path)
    real, calls = w.store._read_batch, []

    def flaky(ref):
        calls.append(ref)
        if len(calls) > 2:   # prelock and pre-lock reads pass; the in-lock one
            raise Refused("evidence_unverified", "batch file changed")  # does not
        return real(ref)

    w.store._read_batch = flaky
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), close_ops(), batch_ref=w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert exc.value.msg.startswith("op 3 close_on_decision demo:A: ")
    assert len(calls) == 3
    assert snap(w) == before                   # edit + evidence rolled back
    item = w.store._item(KEY)
    assert item["state"] == "claimed"
    assert item["review_tier"] == "standard"
    assert item["evidence_kinds"] == ["any"]
    assert item["round"] == 0
    assert w.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    assert w.store.verify_integrity() == []
    assert w.store.verify_chain() == []
    w.close()


# --------------------------------------------- F4: done_criteria / depends_on
def test_router_edits_done_criteria_of_claimed_item(w):
    """F4: the router's active-item exception covers ``done_criteria`` too."""
    epoch = to_claimed(w, KEY)
    out = w.store.edit_definition(w.p("router"), KEY,
                                  {"done_criteria": "tighter criteria"})
    assert out == {"key": KEY, "epoch": epoch + 1}
    item = w.store._item(KEY)
    assert (item["state"], item["epoch"]) == ("claimed", epoch + 1)
    assert item["done_criteria"] == "tighter criteria"
    assert item["round"] == 1          # the same bump the tier edit gets
    event = last_defined(w)
    assert (event["actor"], event["role"]) == ("router", "router")
    assert event["payload"] == {"fields": ["done_criteria"],
                                "while": "claimed", "who": "router",
                                "before": {"done_criteria": "criteria"},
                                "after": {"done_criteria": "tighter criteria"},
                                "new_epoch": epoch + 1}
    assert folded(w)["round"] == item["round"]
    assert folded(w)["epoch"] == item["epoch"]
    assert w.store.verify_integrity() == []
    assert w.store.verify_chain() == []


def test_router_edits_depends_on_of_claimed_item(tmp_path):
    """F4: a claimed item's dependency edges are the router's to rewrite."""
    w = World(tmp_path)
    seed(w, [("A", []), ("B", [])])
    epoch = to_claimed(w, KEY)
    out = w.store.edit_definition(w.p("router"), KEY, {"depends_on": ["demo:B"]})
    assert out == {"key": KEY, "epoch": epoch + 1}
    item = w.store._item(KEY)
    assert (item["state"], item["epoch"]) == ("claimed", epoch + 1)
    assert item["depends_on"] == ["demo:B"]
    edges = {(r["src"], r["dst"]) for r in
             w.conn.execute("SELECT src,dst FROM edges WHERE rel='depends_on'")}
    assert ("demo:A", "demo:B") in edges
    assert item["round"] == 1          # even a depends-on-only edit bumps it
    event = last_defined(w)
    assert event["actor"] == "router"
    assert event["payload"] == {"fields": ["depends_on"],
                                "while": "claimed", "who": "router",
                                "before": {"depends_on": []},
                                "after": {"depends_on": ["demo:B"]},
                                "new_epoch": epoch + 1}
    assert folded(w)["round"] == item["round"]
    assert folded(w)["epoch"] == item["epoch"]
    assert w.store.verify_integrity() == []
    assert w.store.verify_chain() == []
    w.close()


def test_non_router_cannot_edit_done_criteria_or_depends_on_while_claimed(w):
    to_claimed(w, KEY)
    events_before = snap(w)[2]
    for who in ("l1main", "workerA", "reviewer"):
        for fields in ({"done_criteria": "x"}, {"depends_on": ["demo:B"]},
                       {"review_tier": "high", "done_criteria": "x"}):
            with pytest.raises(Refused) as exc:
                w.store.edit_definition(w.p(who), KEY, fields)
            assert exc.value.code == "forbidden", (who, fields)
    # l1main manages demo, so it is the router rule, not authorisation scope,
    # that stops it: the refusal names the exception it is asking for.
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("l1main"), KEY, {"done_criteria": "x"})
    assert "only the router may edit" in exc.value.msg
    item = w.store._item(KEY)
    assert item["state"] == "claimed"
    assert item["done_criteria"] == "criteria"
    assert item["depends_on"] == []
    assert item["round"] == 0
    assert snap(w)[2] == events_before   # no item_defined from any of them
    assert w.store.verify_integrity() == []


def test_router_active_edit_event_carries_before_and_after(tmp_path):
    """Both new fields in one in-review edit: the audit values are exact."""
    w = World(tmp_path)
    seed(w, [("A", []), ("B", [])])
    epoch = to_in_review(w, KEY)               # submit bumped round to 1
    out = w.store.edit_definition(w.p("router"), KEY,
                                  {"done_criteria": "revised",
                                   "depends_on": ["demo:B"]})
    assert out == {"key": KEY, "epoch": epoch + 1}
    item = w.store._item(KEY)
    assert (item["state"], item["epoch"]) == ("in_review", epoch + 1)
    assert item["done_criteria"] == "revised"
    assert item["depends_on"] == ["demo:B"]
    assert item["round"] == 2
    event = last_defined(w)
    assert event["payload"] == {"fields": ["depends_on", "done_criteria"],
                                "while": "in_review", "who": "router",
                                "before": {"done_criteria": "criteria",
                                           "depends_on": []},
                                "after": {"done_criteria": "revised",
                                          "depends_on": ["demo:B"]},
                                "new_epoch": epoch + 1}
    assert folded(w)["round"] == item["round"]
    assert folded(w)["epoch"] == item["epoch"]
    assert w.store.verify_integrity() == []
    assert w.store.verify_chain() == []
    w.close()


def test_other_fields_stay_closed_while_claimed(w):
    """F4 widens the mask to exactly two fields: anything else stays open-only."""
    to_claimed(w, KEY)
    events_before = snap(w)[2]
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), KEY,
                                {"done_criteria": "x", "title": "y"})
    assert exc.value.code == "bad_state"
    assert "not open" in exc.value.msg
    item = w.store._item(KEY)
    assert item["done_criteria"] == "criteria"
    assert item["title"] == "title A"
    assert item["round"] == 0
    assert snap(w)[2] == events_before
    assert w.store.verify_integrity() == []