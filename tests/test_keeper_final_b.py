"""Final-B hardening: write races, reaper reviewer clearing, reachability.

Three items:

1. ``verdict``/``release``/``heartbeat``/``delegate``/``edit_definition``/
   ``add_evidence`` re-read the item inside the write transaction and apply a
   guarded UPDATE, so a competing commit between the pre-read (and, for
   ``verdict(ok)``, the evidence re-verification) and the guarded write is
   refused instead of silently overwriting the winner.
2. ``_reap`` clears ``reviewer`` when it expires a lease, matching
   ``events.fold`` (which clears it on ``lease_expired``).
3. A definition change that introduces a *new* validation problem on a
   pre-existing key (a reachability regression) is refused, while pre-existing
   problems elsewhere keep not blocking unrelated writes.
"""
import pytest

from keeper_util import World, defn, seed, to_claimed, to_in_review
from plangraph.keeper import auth, db as kdb
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _arm(store, competitor):
    """Run *competitor* once, just before *store*'s next write transaction."""
    original = store._write

    def hooked():
        competitor()
        return original()

    store._write = hooked
    return original


def _second(world):
    conn = kdb.open_db(world.path)
    store = Store(conn, clock=world.clock, verifiers=world.verifiers)
    return store, conn


def _count(world, key, kind):
    return world.conn.execute(
        "SELECT COUNT(*) FROM events WHERE key=? AND type=?",
        (key, kind)).fetchone()[0]


# ============================================================== item 1
# ======================================================== verdict races

def test_verdict_ok_race_changes_requested_refused(w):
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    store2, conn2 = _second(w)
    other = auth.authenticate(conn2, w.tok("reviewer"))
    _arm(w.store, lambda: store2.verdict(other, key, False, "changed"))
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), key, True, "ok")
    assert exc.value.code == "conflict"
    assert w.store._item(key)["state"] == "claimed"
    assert _count(w, key, "review_passed") == 0
    conn2.close()


def test_verdict_ok_race_state_change_refused(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_in_review(w, key)
    store2, conn2 = _second(w)

    def competitor():
        # A competing writer moved the item out of review (a release-like
        # change) after the re-verification but before the final commit.
        conn2.execute(
            "UPDATE items SET state='claimed', claimed_by='l1main', epoch=?"
            " WHERE key=?", (epoch + 1, key))
        conn2.commit()

    _arm(w.store, competitor)
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), key, True, "ok")
    assert exc.value.code == "conflict"
    assert _count(w, key, "review_passed") == 0
    conn2.close()


def test_verdict_ok_race_evidence_set_change_refused(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    to_in_review(w, key)
    store2, conn2 = _second(w)
    other = auth.authenticate(conn2, w.tok("router"))
    _arm(w.store, lambda: store2.add_evidence_router(
        other, key, "decision", {"id": "D-LATE"}))
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), key, True, "ok")
    assert exc.value.code == "conflict"
    assert w.store._item(key)["state"] == "in_review"
    assert _count(w, key, "review_passed") == 0
    conn2.close()


# ====================================================== other write races

def test_release_race_refused(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    store2, conn2 = _second(w)
    other = auth.authenticate(conn2, w.tok("l1main"))
    _arm(w.store, lambda: store2.release(other, key, epoch))
    with pytest.raises(Refused) as exc:
        w.store.release(w.p("l1main"), key, epoch)
    assert exc.value.code == "conflict"
    assert w.store._item(key)["state"] == "open"
    conn2.close()


def test_heartbeat_race_refused(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    store2, conn2 = _second(w)
    other = auth.authenticate(conn2, w.tok("l1main"))
    _arm(w.store, lambda: store2.release(other, key, epoch))
    with pytest.raises(Refused) as exc:
        w.store.heartbeat(w.p("l1main"), key, epoch)
    assert exc.value.code == "conflict"
    conn2.close()


def test_delegate_race_refused(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    store2, conn2 = _second(w)
    other = auth.authenticate(conn2, w.tok("l1main"))
    _arm(w.store, lambda: store2.release(other, key, epoch))
    with pytest.raises(Refused) as exc:
        w.store.delegate(w.p("l1main"), key, "workerA")
    assert exc.value.code == "conflict"
    assert w.store._item(key)["delegate"] is None
    conn2.close()


def test_add_evidence_race_refused_and_no_row(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    epoch = to_claimed(w, key)
    store2, conn2 = _second(w)
    other = auth.authenticate(conn2, w.tok("l1main"))
    _arm(w.store, lambda: store2.release(other, key, epoch))
    with pytest.raises(Refused):
        w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    assert w.conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE key=?", (key,)).fetchone()[0] == 0
    conn2.close()


def test_edit_definition_race_refused(w):
    (key,) = seed(w, [("A", [])])
    store2, conn2 = _second(w)
    other = auth.authenticate(conn2, w.tok("l1main"))
    _arm(w.store, lambda: store2.claim(other, key))
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("l1main"), key, {"title": "raced"})
    assert exc.value.code == "conflict"
    assert w.store._item(key)["title"] != "raced"
    conn2.close()


# ============================================================== item 2

def test_reap_clears_reviewer_of_expired_second_review(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_in_review(w, key)
    w.store.verdict(w.p("reviewer"), key, False, "one")
    w.store.heartbeat(w.p("l1main"), key, epoch)
    w.clock.advance(w.store.default_lease + 10)
    w.store.reap(w.p("system"), w.clock())
    item = w.store._item(key)
    assert item["state"] == "open"
    assert item["reviewer"] is None
    assert w.store.verify_integrity() == []


# ============================================================== item 3

def test_edit_reachability_regression_refused(w):
    items = [defn("B", deps=["C"]), defn("C"),
             defn("GOAL", kind="goal", deps=["B"])]
    w.store.create_items(w.p("router"), items)
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("l1main"), "demo:B", {"depends_on": []})
    assert exc.value.code == "validation"
    assert w.store._item("demo:B")["depends_on"] == ["demo:C"]
    assert w.store._item("demo:C")["state"] == "open"


def test_cancel_router_with_dependent_still_allowed(w):
    (key,) = seed(w, [("A", [])])
    w.store.cancel(w.p("router"), key, "drop")
    assert w.store._item(key)["state"] == "cancelled"


def test_edit_pre_existing_problem_elsewhere_allowed(w):
    (key,) = seed(w, [("A", [])])
    w.store.cancel(w.p("router"), key, "drop")
    items = [defn("Z", project="fresh"),
             defn("GOAL", project="fresh", kind="goal", deps=["Z"])]
    w.store.create_items(w.p("router"), items)
    w.store.edit_definition(w.p("router"), "fresh:Z", {"title": "renamed"})
    assert w.store._item("fresh:Z")["title"] == "renamed"
