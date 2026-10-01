"""X5 fixes for the two-reviewer gate (D-219 / D-217).

Three review findings:

1. Family normalisation.  A blank/whitespace ``family`` is *unknown*, not a
   distinct family, and ``teamC``/``TEAMC``/`` teamC `` are the same family.
   ``norm_family`` is the single home for both rules and is applied wherever a
   family is stored or compared.
2. Waiver scope.  ``review_waiver`` belongs to one work epoch: it is cleared by
   ``claim`` (new epoch), ``release`` and ``lease_expired``/reap, in the store
   UPDATEs and in ``events.fold`` so ``verify_integrity`` still agrees.
3. ``digest`` lists pending waivers on ``claimed``/``in_review`` items too, not
   only the ones already ``done``.
"""
import json
import random

import pytest

from keeper_util import World, defn, seed, to_in_review
from plangraph.keeper import auth
from plangraph.keeper import events as keeper_events
from plangraph.keeper import fmt
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


KEY = "demo:A"
DECISION = "D-217"


def _reviewer(world, name, family):
    token = auth.add_principal(world.conn, name, "reviewer", ["demo"],
                               family=family)
    return auth.authenticate(world.conn, token)


def _raw_reviewer(world, name, family, token):
    """Insert a principal directly, as a legacy/migrated row would be."""
    world.conn.execute(
        "INSERT INTO principals(name,role,projects,family,token_sha256,active)"
        " VALUES(?,?,?,?,?,1)",
        (name, "reviewer", json.dumps(["demo"]), family,
         auth.hash_token(token)))
    return auth.authenticate(world.conn, token)


def _stored_family(world, name):
    return world.conn.execute(
        "SELECT family FROM principals WHERE name=?", (name,)).fetchone()[0]


# ------------------------------------------------ item 1: family normalisation

def test_add_principal_stores_normalised_family(w):
    mixed = "  TeamC  "
    auth.add_principal(w.conn, "mixed", "reviewer", ["demo"], family=mixed)
    auth.add_principal(w.conn, "blank", "reviewer", ["demo"], family="   ")
    assert _stored_family(w, "mixed") == "teamc"
    assert _stored_family(w, "blank") is None


def test_stored_family_is_normalised_when_read(w):
    """A legacy row's ``TeamC`` must compare as ``teamc``."""
    _raw_reviewer(w, "legacyrev", "TeamC", "legacy-token")
    assert auth.get_principal(w.conn, "legacyrev").family == "teamc"


def test_blank_family_reviewer_refused_without_waiver(w):
    to_in_review(w, KEY)
    blank = _reviewer(w, "blankFam", "   ")
    with pytest.raises(Refused) as exc:
        w.store.verdict(blank, KEY, True, "who am I?")
    assert exc.value.code == "family_unknown"
    assert w.store._item(KEY)["state"] == "in_review"


def test_blank_family_reviewer_refused_under_waiver(w):
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    blank = _reviewer(w, "blankFam", "")
    with pytest.raises(Refused) as exc:
        w.store.verdict(blank, KEY, True, "waived but anonymous")
    assert exc.value.code == "family_unknown"
    assert w.store._item(KEY)["state"] == "in_review"


def test_blank_owner_family_treated_as_unknown(w):
    items = [defn("B", [], state="in_review", owner="legacy",
                  owner_family="  "),
             defn("GOALB", ["demo:B"], kind="goal")]
    w.store.import_items(w.p("router"), items)
    item = w.store._item("demo:B")
    assert item["owner_family"] is None
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), "demo:B", True, "legacy review")
    assert exc.value.code == "family_unknown"


def test_blank_delegate_family_treated_as_unknown(w):
    epoch = w.store.claim(w.p("workerA"), KEY)["epoch"]
    auth.add_principal(w.conn, "blankworker", "worker", ["demo"], family="   ")
    w.store.delegate(w.p("workerA"), KEY, "blankworker")
    w.store.add_evidence(w.p("workerA"), KEY, epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("workerA"), KEY, epoch)
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), KEY, True, "delegated")
    assert exc.value.code == "family_unknown"


@pytest.mark.parametrize("second_family", ["TEAMC", " teamC ", "teamc"])
def test_same_family_case_differences_refused(w, second_family):
    to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")  # family teamC
    twin = _reviewer(w, "twin", second_family)
    with pytest.raises(Refused) as exc:
        w.store.verdict(twin, KEY, True, "same team, different spelling")
    assert exc.value.code == "same_family"
    assert w.store._item(KEY)["state"] == "in_review"


def test_case_insensitive_family_reaches_done_under_waiver(w):
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    twin = _reviewer(w, "twin", " TEAMC ")
    result = w.store.verdict(twin, KEY, True, "waived")
    assert result["state"] == "done"
    assert w.store.verify_integrity() == []


def test_stored_legacy_family_compares_equal_to_normalised(w):
    """The second reviewer's stored ``TeamC`` equals the first's ``teamC``."""
    to_in_review(w, KEY)
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")  # normalised teamc
    legacy = _raw_reviewer(w, "legacyrev", "TeamC", "legacy-token")
    with pytest.raises(Refused) as exc:
        w.store.verdict(legacy, KEY, True, "same family, old row")
    assert exc.value.code == "same_family"


# ------------------------------------------------------ item 2: waiver scope

def test_release_clears_waiver_then_reclaim_refuses_same_family(w):
    epoch = w.store.claim(w.p("l1main"), KEY)["epoch"]
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    assert w.store._item(KEY)["review_waiver"] == DECISION
    w.store.release(w.p("l1main"), KEY, epoch)
    assert w.store._item(KEY)["review_waiver"] is None

    epoch2 = w.store.claim(w.p("workerA"), KEY)["epoch"]
    assert epoch2 != epoch
    w.store.add_evidence(w.p("workerA"), KEY, epoch2, "decision",
                         {"id": "D-1"})
    w.store.submit(w.p("workerA"), KEY, epoch2)
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    twin = _reviewer(w, "twin", "teamC")
    with pytest.raises(Refused) as exc:
        w.store.verdict(twin, KEY, True, "still waived?")
    assert exc.value.code == "same_family"
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


def test_reap_clears_waiver(w):
    w.store.claim(w.p("l1main"), KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    w.clock.advance(7201)
    result = w.store.reap(w.p("system"), w.clock())
    assert KEY in result["expired"]
    assert w.store._item(KEY)["state"] == "open"
    assert w.store._item(KEY)["review_waiver"] is None
    assert w.store.verify_integrity() == []


def test_claim_new_epoch_clears_waiver(w):
    """A waiver set in review survives one change, but not the next epoch."""
    epoch = to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    # First change returns to claimed in the same epoch: the waiver stays.
    w.store.verdict(w.p("reviewer"), KEY, False, "change one")
    assert w.store._item(KEY)["review_waiver"] == DECISION
    w.store.submit(w.p("l1main"), KEY, epoch)
    # Second change returns the item to the open pool in the same epoch too.
    w.store.verdict(w.p("reviewer"), KEY, False, "change two")
    assert w.store._item(KEY)["state"] == "open"
    assert w.store._item(KEY)["review_waiver"] == DECISION
    # A fresh claim opens a new epoch and drops it.
    w.store.claim(w.p("workerA"), KEY)
    assert w.store._item(KEY)["review_waiver"] is None
    assert w.store.verify_integrity() == []


def test_fold_clears_waiver_on_epoch_end(w):
    epoch = w.store.claim(w.p("l1main"), KEY)["epoch"]
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    w.store.release(w.p("l1main"), KEY, epoch)
    folded = keeper_events.fold(keeper_events.read_events(w.conn))
    assert folded[KEY]["review_waiver"] is None
    assert w.store._item(KEY)["review_waiver"] is None


def test_random_walk_with_waivers_keeps_integrity(w):
    rng = random.Random(0x5EEDF00D)
    for _ in range(60):
        item = w.store._item(KEY)
        state = item["state"]
        if state == "open":
            try:
                w.store.claim(w.p("l1main"), KEY)
            except Refused:
                pass
        elif state == "claimed":
            action = rng.choice(["waive", "release", "reap", "evidence",
                                 "submit"])
            if action == "waive":
                try:
                    w.store.waive_second_family(w.p("router"), KEY, DECISION)
                except Refused:
                    pass
            elif action == "release":
                try:
                    w.store.release(w.p("l1main"), KEY, item["epoch"])
                except Refused:
                    pass
            elif action == "reap":
                w.store.reap(w.p("system"), w.clock())
            elif action == "evidence":
                try:
                    w.store.add_evidence(w.p("l1main"), KEY, item["epoch"],
                                         "decision", {"id": "D-1"})
                except Refused:
                    pass
            else:
                try:
                    w.store.submit(w.p("l1main"), KEY, item["epoch"])
                except Refused:
                    pass
        elif state == "in_review":
            if rng.random() < 0.5:
                try:
                    w.store.waive_second_family(w.p("router"), KEY, DECISION)
                except Refused:
                    pass
            else:
                try:
                    w.store.verdict(w.p("reviewer"), KEY, False, "walk")
                except Refused:
                    pass
        w.clock.advance(100)
    assert w.store.verify_chain() == []
    assert w.store.verify_integrity() == []


# ------------------------------------------------------------ item 3: digest

def test_digest_lists_pending_waiver_in_review(w):
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    digest = w.store.digest(w.p("router"))
    assert {"key": KEY, "decision_id": DECISION, "state": "in_review"} in \
        digest["waivers"]
    assert DECISION in fmt.digest_text(digest)


def test_digest_lists_pending_waiver_claimed(w):
    w.store.claim(w.p("l1main"), KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    digest = w.store.digest(w.p("router"))
    assert {"key": KEY, "decision_id": DECISION, "state": "claimed"} in \
        digest["waivers"]


def test_digest_keeps_done_waiver_shape(w):
    to_in_review(w, KEY)
    w.store.waive_second_family(w.p("router"), KEY, DECISION)
    twin = _reviewer(w, "twin", "teamC")
    w.store.verdict(w.p("reviewer"), KEY, True, "ok")
    w.store.verdict(twin, KEY, True, "ok")
    digest = w.store.digest(w.p("router"))
    assert {"key": KEY, "decision_id": DECISION} in digest["waivers"]
