"""State-machine tests for the keeper Store (allowed and disallowed edges)."""
import pytest

from keeper_util import World, FakeVerifier, seed, to_claimed, to_done, to_in_review
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


# ------------------------------------------------------------- allowed edges

def test_open_to_claimed(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    item = w.store._item(key)
    assert item["state"] == "claimed"
    assert item["claimed_by"] == "l1main"
    assert item["epoch"] == epoch == 1


def test_claimed_to_open_via_release(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    w.store.release(w.p("l1main"), key, epoch)
    assert w.store._item(key)["state"] == "open"
    assert w.store._item(key)["claimed_by"] is None


def test_claimed_to_in_review_via_submit(w):
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    assert w.store._item(key)["state"] == "in_review"


def test_in_review_to_claimed_via_changes(w):
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    w.store.verdict(w.p("reviewer"), key, False, "needs work")
    item = w.store._item(key)
    assert item["state"] == "claimed"
    assert item["reviewer"] == "reviewer"


def test_in_review_to_done_via_pass(w):
    (key,) = seed(w, [("A", [])])
    to_done(w, key)
    assert w.store._item(key)["state"] == "done"


@pytest.mark.parametrize("builder", ["open", "claimed", "in_review"])
def test_cancel_from_active_states(w, builder):
    (key,) = seed(w, [("A", [])])
    if builder == "claimed":
        to_claimed(w, key)
    elif builder == "in_review":
        to_in_review(w, key)
    w.store.cancel(w.p("router"), key, "drop it")
    assert w.store._item(key)["state"] == "cancelled"


# ---------------------------------------------------------- disallowed edges

def test_submit_from_open_refused(w):
    (key,) = seed(w, [("A", [])])
    with pytest.raises(Refused) as exc:
        w.store.submit(w.p("l1main"), key, 0)
    assert exc.value.code == "bad_state"


def test_claim_twice_is_conflict(w):
    (key,) = seed(w, [("A", [])])
    to_claimed(w, key)
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("workerA"), key)
    assert exc.value.code == "conflict"


def test_verdict_from_claimed_refused(w):
    (key,) = seed(w, [("A", [])])
    to_claimed(w, key)
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("reviewer"), key, True, "nope")
    assert exc.value.code == "bad_state"


def test_release_from_in_review_refused(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_in_review(w, key)
    with pytest.raises(Refused) as exc:
        w.store.release(w.p("l1main"), key, epoch)
    assert exc.value.code == "bad_state"


def test_claim_blocked_by_unfinished_dep(w):
    seed(w, [("A", []), ("B", ["A"])])
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:B")
    assert exc.value.code == "bad_state"


# ------------------------------------------------------------------ locking

def test_done_row_is_locked_for_every_verb(w):
    (key,) = seed(w, [("A", [])])
    to_done(w, key)
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("workerA"), key)
    assert exc.value.code == "locked"
    with pytest.raises(Refused) as exc:
        w.store.cancel(w.p("router"), key, "too late")
    assert exc.value.code == "locked"
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), key, {"title": "new"})
    assert exc.value.code == "locked"


def test_cancelled_row_is_locked(w):
    (key,) = seed(w, [("A", [])])
    w.store.cancel(w.p("router"), key, "drop")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("workerA"), key)
    assert exc.value.code == "locked"
    with pytest.raises(Refused) as exc:
        w.store.cancel(w.p("router"), key, "again")
    assert exc.value.code == "locked"


def test_unknown_key_not_found(w):
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:GHOST")
    assert exc.value.code == "not_found"


# -------------------------------------------------------------- stale epochs

@pytest.mark.parametrize("verb", ["heartbeat", "submit", "release"])
def test_stale_epoch_refused(w, verb):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    if verb == "submit":
        w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    with pytest.raises(Refused) as exc:
        getattr(w.store, verb)(w.p("l1main"), key, epoch + 5)
    assert exc.value.code == "epoch_stale"


# ------------------------------------------------------------------ reaper

def test_reap_expires_lease(w):
    (key,) = seed(w, [("A", [])])
    to_claimed(w, key, lease_s=100)
    w.clock.advance(101)
    w.store.reap(w.p("system"), w.clock())
    assert w.store._item(key)["state"] == "open"


def test_reap_keeps_live_lease(w):
    (key,) = seed(w, [("A", [])])
    to_claimed(w, key, lease_s=1000)
    w.clock.advance(10)
    w.store.reap(w.p("system"), w.clock())
    assert w.store._item(key)["state"] == "claimed"


def test_reap_flags_overdue_review(w):
    (key,) = seed(w, [("A", [])])
    to_in_review(w, key)
    w.clock.advance(4 * 3600 + 1)
    w.store.reap(w.p("system"), w.clock())
    findings = w.conn.execute(
        "SELECT code FROM audit_findings WHERE key=?", (key,)).fetchall()
    assert [r[0] for r in findings] == ["review_overdue"]
    assert w.store._item(key)["state"] == "in_review"


# ---------------------------------------------------------- second rejection

def test_second_rejection_reopens_and_finds(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_in_review(w, key)
    w.store.verdict(w.p("reviewer"), key, False, "one")
    w.store.submit(w.p("l1main"), key, epoch)
    w.store.verdict(w.p("reviewer"), key, False, "two")
    assert w.store._item(key)["state"] == "open"
    findings = w.conn.execute(
        "SELECT code FROM audit_findings WHERE key=?", (key,)).fetchall()
    assert [r[0] for r in findings] == ["changes_requested_twice"]


# ---------------------------------------------------------------- heartbeat

def test_heartbeat_extends_lease(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key, lease_s=100)
    before = w.store._item(key)["lease_until"]
    w.clock.advance(50)
    w.store.heartbeat(w.p("l1main"), key, epoch)
    assert w.store._item(key)["lease_until"] != before


# ------------------------------------------------------------------ import

def test_import_legacy_claimed_and_in_review(w):
    items = [
        {"project": "demo", "id": "A", "title": "a", "kind": "lane",
         "state": "claimed", "owner": "someone@example.invalid",
         "depends_on": [], "evidence_kinds": ["any"]},
        {"project": "demo", "id": "B", "title": "b", "kind": "lane",
         "state": "in_review", "owner": "someone@example.invalid",
         "depends_on": [], "evidence_kinds": ["any"]},
        {"project": "demo", "id": "GOAL", "title": "g", "kind": "goal",
         "state": "open", "depends_on": ["demo:A", "demo:B"],
         "evidence_kinds": ["any"]},
    ]
    w.store.import_items(w.p("router"), items)
    a = w.store._item("demo:A")
    b = w.store._item("demo:B")
    assert a["state"] == "claimed" and a["claimed_by"] == "someone@example.invalid"
    assert b["state"] == "in_review"
    # legacy rows need no verified evidence until they are submitted again
    assert w.store.verify_integrity() == []


def test_import_done_is_refused(w):
    items = [{"project": "demo", "id": "A", "title": "a", "kind": "lane",
              "state": "done", "depends_on": [], "evidence_kinds": ["any"]},
             {"project": "demo", "id": "GOAL", "title": "g", "kind": "goal",
              "state": "open", "depends_on": ["demo:A"], "evidence_kinds": ["any"]}]
    with pytest.raises(Refused) as exc:
        w.store.import_items(w.p("router"), items)
    assert exc.value.code == "validation"


def test_import_requires_router(w):
    items = [{"project": "demo", "id": "A", "title": "a", "kind": "lane",
              "state": "open", "depends_on": [], "evidence_kinds": ["any"]}]
    with pytest.raises(Refused) as exc:
        w.store.import_items(w.p("l1main"), items)
    assert exc.value.code == "forbidden"
