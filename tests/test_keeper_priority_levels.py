"""KEEPER-PRIORITY-LEVELS-AND-AGING: LE items start at 8, a repeat INCIDENT LOG line
raises one level, an unclaimed 10 (3 days) or 9 (7 days) drops one level unless
re-confirmed, and the digest counts the levels."""
import json

import pytest

from keeper_util import World, defn, seed, to_claimed

DAY = 86400
LOG1 = "INCIDENT LOG 2026-11-15T05:10Z (plan-order-0540): first time"
LOG2 = "INCIDENT LOG 2026-11-18T09:00Z (plan-order-0600): second time"
LOG3 = "INCIDENT LOG 2026-11-19T09:00Z (plan-order-0610): third time"


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


def prio(w, key):
    return w.store._item(key)["priority"]


def make(w, pid, priority=None, **kw):
    extra = {} if priority is None else {"priority": priority}
    w.store.create_items(w.p("router"), [defn(pid, under="GOAL", **extra, **kw)])
    return f"demo:{pid}"


def aged(w, advance_s):
    w.clock.advance(advance_s)
    return w.store.reap(w.p("system"))["priority_aged"]


def events(w, key, kind="priority_set"):
    return [json.loads(r[0]) for r in w.conn.execute(
        "SELECT payload FROM events WHERE key=? AND type=? ORDER BY seq", (key, kind))]


# ------------------------------------------------------------------ create
def test_an_le_item_without_a_priority_starts_at_8(w):
    assert prio(w, make(w, "LE-SOMETHING", description=LOG1)) == 8


def test_an_explicit_priority_is_kept_even_zero(w):
    assert prio(w, make(w, "LE-TEN", 10)) == 10
    assert prio(w, make(w, "LE-ZERO", 0)) == 0


def test_other_items_keep_the_default_zero(w):
    assert prio(w, make(w, "PLAIN")) == 0


def test_the_le_default_holds_through_apply_ops_and_for_an_l1(w):
    out = w.store.apply_ops(w.p("router"), [
        {"op": "create", "item": defn("LE-VIA-BATCH", under="GOAL")}])
    assert out["applied"] == 1 and prio(w, "demo:LE-VIA-BATCH") == 8
    w.store.create_items(w.p("l1main"), [defn("LE-L1", under="GOAL")])
    assert prio(w, "demo:LE-L1") == 8


# ------------------------------------------------------------------ repeat
def test_a_new_incident_log_line_raises_one_level_up_to_ten(w):
    key = make(w, "LE-REPEAT", description=LOG1)
    assert prio(w, key) == 8
    router = w.p("router")
    w.store.edit_definition(router, key, {"description": f"{LOG1}\n{LOG2}"})
    assert prio(w, key) == 9
    event, = events(w, key)[-1:]
    assert event["priority"] == 9 and "incident" in event["reason"].lower()
    w.store.edit_definition(router, key, {"description": f"{LOG1}\n{LOG2}\n{LOG3}"})
    assert prio(w, key) == 10
    w.store.edit_definition(router, key, {
        "description": f"{LOG1}\n{LOG2}\n{LOG3}\nINCIDENT LOG 2026-11-20: again"})
    assert prio(w, key) == 10                      # capped


def test_an_edit_without_a_new_line_or_on_a_plain_item_changes_nothing(w):
    key = make(w, "LE-STILL", description=LOG1)
    w.store.edit_definition(w.p("router"), key,
                            {"description": f"{LOG1}\nsome other prose"})
    assert prio(w, key) == 8
    plain = make(w, "PLAIN2", description=LOG1)
    w.store.edit_definition(w.p("router"), plain, {"description": f"{LOG1}\n{LOG2}"})
    assert prio(w, plain) == 0


# ------------------------------------------------------------------- aging
def test_an_unclaimed_ten_older_than_three_days_drops_one_level(w):
    key = make(w, "LE-TEN", 10)
    assert aged(w, 2 * DAY) == []
    assert prio(w, key) == 10
    assert aged(w, 2 * DAY) == [{"key": key, "from": 10, "to": 9}]
    assert prio(w, key) == 9
    reason = events(w, key)[-1]["reason"]
    assert "not claimed in 3 days" in reason and "10" in reason
    assert aged(w, 2 * DAY) == []                  # the clock restarted at the drop


def test_an_unclaimed_nine_older_than_seven_days_drops_one_level(w):
    key = make(w, "LE-NINE", 9)
    assert aged(w, 5 * DAY) == []
    assert aged(w, 3 * DAY) == [{"key": key, "from": 9, "to": 8}]
    assert "7 days" in events(w, key)[-1]["reason"]
    assert aged(w, 30 * DAY) == []                 # 8 and below never age


def test_a_re_confirmed_priority_restarts_the_clock(w):
    key = make(w, "LE-CONFIRM", 10)
    w.clock.advance(2 * DAY)
    w.store.set_priority(w.p("router"), key, 10)
    assert aged(w, 2 * DAY) == []
    assert prio(w, key) == 10
    assert aged(w, 2 * DAY) == [{"key": key, "from": 10, "to": 9}]


def test_a_claimed_item_does_not_age(w):
    key = make(w, "LE-CLAIMED", 10)
    to_claimed(w, key, lease_s=30 * DAY)
    assert aged(w, 10 * DAY) == []
    assert prio(w, key) == 10


def test_aging_is_idempotent_within_one_tick_and_leaves_integrity(w):
    key = make(w, "LE-IDEMP", 10)
    w.clock.advance(4 * DAY)
    first = w.store.reap(w.p("system"))["priority_aged"]
    second = w.store.reap(w.p("system"))["priority_aged"]
    assert len(first) == 1 and second == []
    assert prio(w, key) == 9
    assert w.store.verify_integrity() == []


# ------------------------------------------------------------------ digest
def test_digest_counts_open_items_per_level_and_warns_on_a_crowded_ten(w):
    for i in range(3):
        make(w, f"LE-T{i}", 10)
    make(w, "LE-N", 9)
    out = w.store.digest(w.p("router"))
    assert out["priority_levels"]["10"] == 3 and out["priority_levels"]["9"] == 1
    assert out["level10_warning"]["open"] == 3
    assert out["level10_warning"]["lanes_closed_7d"] == 0
    from plangraph.keeper import fmt
    text = fmt.digest_text(out)
    assert "priority 10=3 9=1" in text
    assert "WARN level 10 holds 3 open items" in text and "0 lanes closed" in text


def test_digest_has_no_warning_when_closed_lanes_cover_level_ten(tmp_path):
    from test_keeper_close import CLOSES, _world
    w = _world(tmp_path, CLOSES)
    w.store.close_on_decision(w.p("router"), "demo:A", w.batch_ref)
    w.store.create_items(w.p("router"), [defn("LE-ONE", under="GOAL", priority=10)])
    out = w.store.digest(w.p("router"))
    assert out["priority_levels"]["10"] == 1
    assert out["level10_warning"] is None
    w.close()
