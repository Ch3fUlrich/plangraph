"""Hermetic oversight checks: rules R1..R7 of the flow-oversight spec.

One test that fires and one that does not for each rule, plus R1/R2 parity
against ``keeper.stall.stall_alarms`` on the same fixture (the F5 shapes: a
stalled claim and a ``Plan-Item`` trailer merged on main).

Every test body opens with ``import plangraph.keeper.oversight`` and then
``from plangraph.keeper import oversight``, both *inside* the test, so the rule
under test is what demands the module.  The direct submodule import is what
turns a missing ``oversight.py`` into a ``ModuleNotFoundError`` - the
from-import on its own reports a plain ``ImportError`` - so while the module
does not exist every test is RED for exactly one reason and nothing else.
No network, no wall clock, no real git: time comes from ``keeper_util.World``
and git is always an injected callable.
"""
from __future__ import annotations

import re

import pytest

from keeper_util import World, defn, seed, to_claimed, to_done
from plangraph.keeper.events import append_event
from plangraph.keeper.stall import stall_alarms

HOUR = 3600.0


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def event(w, key, ts=None):
    """A heartbeat for *key* stamped *ts* (default: the current clock)."""
    append_event(w.conn, w.store._iso(w.clock() if ts is None else ts),
                 "router", "router", None, key, "heartbeat", {})
    w.conn.commit()


def kinds(findings):
    """The ``(rule, kind, key)`` triples every assertion compares."""
    return [(f["rule"], f["kind"], f["key"]) for f in findings]


# --------------------------------------------------------- R1 stall_no_event
def test_r1_fires_on_a_claimed_item_whose_newest_event_is_stale(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    key = seed(w, [("A", ()), ("B", ())])[0]
    to_claimed(w, key, lease_s=10 * HOUR)
    event(w, key)
    w.clock.advance(3 * HOUR)
    rows = oversight.r1_r2(w.conn, w.clock())
    assert kinds(rows) == [("R1", "stall_no_event", key)]
    assert rows[0]["reason"] == f"R1:stall_no_event:{key}"
    assert "10800" in rows[0]["detail"]     # the stall detail rides along


def test_r1_does_not_fire_when_the_claim_has_a_recent_heartbeat(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    key = seed(w, [("A", ()), ("B", ())])[0]
    to_claimed(w, key, lease_s=10 * HOUR)
    event(w, key)
    w.clock.advance(1 * HOUR)               # still inside the 2h window
    assert oversight.r1_r2(w.conn, w.clock()) == []


# ---------------------------------------------------------- R2 merged_open
def test_r2_fires_on_an_open_item_a_merged_main_trailer_claims(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    key = seed(w, [("A", ()), ("B", ())])[0]
    git = lambda repo, ref: [("deadbeef", key)]
    rows = oversight.r1_r2(w.conn, w.clock(), git=git)
    assert kinds(rows) == [("R2", "merged_open", key)]
    assert rows[0]["reason"] == f"R2:merged_open:{key}"
    assert rows[0]["detail"].startswith("merged commit ")


def test_r2_does_not_fire_for_a_trailer_on_a_closed_item(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    key = seed(w, [("A", ()), ("B", ())])[0]
    to_done(w, key)                          # a closed item is not open work
    git = lambda repo, ref: [("deadbeef", key)]
    assert oversight.r1_r2(w.conn, w.clock(), git=git) == []


# --------------------------------------------------------- R3 critical_idle
def test_r3_fires_on_an_idle_open_item_with_done_deps_on_a_gate_path(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    # demo:A has no deps, feeds demo:B and the goal, and never moves again.
    keys = seed(w, [("A", ()), ("B", ("A",))])
    w.clock.advance(oversight.STALL_S + HOUR)
    rows = oversight.r3_critical_idle(w.conn, w.clock())
    assert kinds(rows) == [("R3", "critical_idle", keys[0])]
    assert rows[0]["reason"] == f"R3:critical_idle:{keys[0]}"


def test_r3_skips_that_idle_item_while_it_is_claimed(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    # Claimed work belongs to R1: flip that one condition, R3 stays quiet.
    keys = seed(w, [("A", ()), ("B", ("A",))])
    to_claimed(w, keys[0], lease_s=10 * HOUR)
    w.clock.advance(oversight.STALL_S + HOUR)
    assert oversight.r3_critical_idle(w.conn, w.clock()) == []


# ----------------------------------------------------------- R4 lane_idle
_LANE_REF = "refs/remotes/origin/lane/demo-a"
_LANE_SHORT = "origin/lane/demo-a"


def lane_git(commit_ts, ahead="5"):
    """The ``r4_lane_idle`` git contract: a callable ``git(args) -> str``.

    It answers ``for-each-ref`` (refname + committerdate, honouring whatever
    ``--format`` placeholders the rule asks for) and ``rev-list --count``,
    so no repository and no git process is ever needed.  ``lane/demo-a`` is
    the branch whose slug (``demo-a``) matches the item key ``demo:A``.
    """
    stamp = str(int(commit_ts))

    def git(args):
        if "for-each-ref" in args:
            fmt = ""
            for i, arg in enumerate(args):
                if arg.startswith("--format="):
                    fmt = arg.split("=", 1)[1]
                elif arg == "--format" and i + 1 < len(args):
                    fmt = args[i + 1]
            fmt = fmt or "%(refname) %(committerdate:unix)"
            line = re.sub(r"%\(refname(?::[^)]*)?\)",
                          lambda m: _LANE_SHORT if "short" in m.group(0)
                          else _LANE_REF, fmt)
            return re.sub(r"%\(committerdate(?::[^)]*)?\)", stamp, line) + "\n"
        if "rev-list" in args:               # ahead of main, by one lane
            return f"{ahead}\n"
        return ""

    return git


def test_r4_fires_on_a_lane_branch_that_has_fallen_idle(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    key = seed(w, [("A", ())])[0]            # the item lane/demo-a maps onto
    w.clock.advance(oversight.LANE_IDLE_S + HOUR)
    git = lane_git(w.clock() - (oversight.LANE_IDLE_S + HOUR))
    rows = oversight.r4_lane_idle(w.conn, w.clock(), git=git)
    assert kinds(rows) == [("R4", "lane_idle", key)]
    assert rows[0]["reason"] == f"R4:lane_idle:{key}"


def test_r4_does_not_fire_while_the_lane_commit_is_recent(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    seed(w, [("A", ())])
    w.clock.advance(oversight.LANE_IDLE_S + HOUR)   # the item itself is stale
    git = lane_git(w.clock() - HOUR)                # ...but the branch moved
    assert oversight.r4_lane_idle(w.conn, w.clock(), git=git) == []


# ----------------------------------------------------------- R5 windows
def seed_needs_you(w):
    """An open operator item, a plain lane, and the goal joining them."""
    w.store.create_items(w.p("router"), [
        defn("A", (), kind="operator_task", operator=True),
        defn("B", ()),
        defn("GOAL", ["A", "B"], kind="goal"),
    ])


def ask(w, key, deadline):
    """An open question for *key* whose *deadline* is an ISO stamp."""
    w.conn.execute(
        "INSERT INTO questions(id, key, text, deadline, state, created_at)"
        " VALUES((SELECT COALESCE(MAX(id),0)+1 FROM questions),?,?,?,?,?)",
        (key, "which way?", deadline, "open", w.store._iso(w.clock())))
    w.conn.commit()


def test_r5_fires_on_an_overdue_question_and_an_aged_inbox_item(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    seed_needs_you(w)
    ask(w, "demo:B", w.store._iso(w.clock() - HOUR))    # deadline already past
    w.clock.advance(oversight.INBOX_WINDOW_S + HOUR)    # inbox age past window
    rows = oversight.r5_windows(w.conn, w.clock())
    assert kinds(rows) == [("R5", "inbox_overdue", "demo:A"),
                           ("R5", "question_overdue", "demo:B")]
    assert all(f["reason"] == f"R5:{f['kind']}:{f['key']}" for f in rows)


def test_r5_does_not_fire_inside_both_windows(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    seed_needs_you(w)
    ask(w, "demo:B", w.store._iso(w.clock() + HOUR))     # still to be answered
    assert oversight.r5_windows(w.conn, w.clock()) == [] # inbox age is 0


# ------------------------------------------------------- R6 projector
def watermark(w, updated_at):
    """Put the ``*`` projector watermark at *updated_at*, creating it if need."""
    cur = w.conn.execute("UPDATE projector_watermark SET updated_at=?"
                         " WHERE workflow_id='*'", (updated_at,))
    if cur.rowcount == 0:
        w.conn.execute("INSERT INTO projector_watermark(workflow_id, updated_at)"
                       " VALUES('*', ?)", (updated_at,))
    w.conn.commit()


def test_r6_fires_on_a_stale_watermark_and_an_open_reconcile_alarm(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    watermark(w, w.store._iso(w.clock() - 2 * oversight.PROJECTOR_STALE_S))
    w.conn.execute(
        "INSERT INTO projector_alarms(id, kind, workflow_id, key, detail,"
        " raised_at) VALUES(9, 'state_drift', 'wf', 'demo:Z', 'drifted', ?)",
        (w.store._iso(w.clock()),))
    w.conn.commit()
    rows = oversight.r6_projector(w.conn, w.clock())
    assert {(f["rule"], f["kind"]) for f in rows} == {
        ("R6", "watermark_stale"), ("R6", "reconcile_alarm")}
    stale = [f for f in rows if f["kind"] == "watermark_stale"]
    assert len(stale) == 1 and stale[0]["rule"] == "R6"
    alarm = [f for f in rows if f["kind"] == "reconcile_alarm"]
    assert [(f["rule"], f["key"]) for f in alarm] == [
        ("R6", "state_drift:demo:Z")]
    assert alarm[0]["reason"].startswith("R6:") and "demo:Z" in alarm[0]["reason"]


def test_r6_does_not_fire_with_a_fresh_watermark_and_no_open_alarm(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    watermark(w, w.store._iso(w.clock()))
    w.conn.execute(
        "INSERT INTO projector_alarms(id, kind, workflow_id, key, detail,"
        " raised_at, cleared_at) VALUES(9, 'state_drift', 'wf', 'demo:Z',"
        " 'drifted', ?, ?)", (w.store._iso(w.clock()), w.store._iso(w.clock())))
    w.conn.commit()
    assert oversight.r6_projector(w.conn, w.clock()) == []


# ----------------------------------------------------------- R7 burn
DAY = "2026-10-22"


def metric(w, day, value):
    """A ``claude_burn_index`` row for *day* holding *value*."""
    w.conn.execute(
        "INSERT INTO metrics(day, metric, value, source, computed_at)"
        " VALUES(?,?,?,?,?)",
        (day, "claude_burn_index", float(value), "test",
         w.store._iso(w.clock())))
    w.conn.commit()


def test_r7_fires_when_the_burn_index_is_over_the_limit(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    metric(w, DAY, oversight.BURN_LIMIT + 1)
    rows = oversight.r7_burn(w.conn, DAY, w.clock())
    assert [(f["rule"], f["kind"]) for f in rows] == [("R7", "burn_over")]
    assert rows[0]["reason"].startswith("R7:burn_over:")


def test_r7_does_not_fire_at_the_limit_or_without_a_row(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    metric(w, DAY, oversight.BURN_LIMIT)     # equal is not over the limit
    assert oversight.r7_burn(w.conn, DAY, w.clock()) == []
    assert oversight.r7_burn(w.conn, "2026-10-21", w.clock()) == []  # no row


# --------------------------------------------- R1/R2 parity with stall_alarms
def parity(w, git=None):
    """``r1_r2`` must reproduce ``stall_alarms`` here, rule id aside."""
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    rows = stall_alarms(w.conn, w.clock(), git=git)
    found = oversight.r1_r2(w.conn, w.clock(), git=git)
    rule_of = {"stall_no_event": "R1", "merged_open": "R2"}
    expected = sorted((rule_of[r["kind"]], r["kind"], r["key"], r["detail"],
                       r["raised_at"]) for r in rows)
    actual = sorted((f["rule"], f["kind"], f["key"], f["detail"], f["ts"])
                    for f in found)
    assert actual == expected


def test_r1_r2_match_stall_alarms_on_the_same_fixture(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    keys = seed(w, [("A", ()), ("B", ())])
    to_claimed(w, keys[0], lease_s=10 * HOUR)
    event(w, keys[0])
    w.clock.advance(3 * HOUR)
    git = lambda repo, ref: [("deadbeef", keys[0]), ("c0ffee", keys[1])]
    found = oversight.r1_r2(w.conn, w.clock(), git=git)
    assert {(f["rule"], f["kind"]) for f in found} == {
        ("R1", "stall_no_event"), ("R2", "merged_open")}
    parity(w, git)

    event(w, keys[0])                      # heartbeat inside the window
    to_done(w, keys[1])                    # the merged lane is now closed
    git = lambda repo, ref: [("c0ffee", keys[1])]
    assert stall_alarms(w.conn, w.clock(), git=git) == []
    parity(w, git)


# ------------------------------- run_checks threading (R1/R2 vs R4 git shapes)
def test_run_checks_gives_r4_lane_git_while_r1_r2_keep_the_config_git(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    key = seed(w, [("A", ())])[0]
    w.clock.advance(oversight.LANE_IDLE_S + HOUR)
    git = lane_git(w.clock() - (oversight.LANE_IDLE_S + HOUR))
    rows = oversight.run_checks(w.conn, w.clock(), lane_git=git)
    assert ("R4", "lane_idle", key) in kinds(rows)
    # A config-style object handed to `git=` (stall's contract) must not be
    # mistaken for r4's `git(args) -> str`: only lane_git reaches R4.
    assert "R4" not in {f["rule"] for f in oversight.run_checks(
        w.conn, w.clock(), git=object())}


# ------------------------------------------- F2: no wall clock in the digest
def test_digest_does_not_change_just_because_time_passes(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    w.store.create_items(w.p("router"), [
        defn("A", ()),
        defn("OP", (), kind="operator_task", operator=True),
        defn("GOAL", ["A", "OP"], kind="goal"),
    ])
    ask(w, "demo:A", w.store._iso(w.clock() - HOUR))    # overdue already
    w.clock.advance(oversight.INBOX_WINDOW_S + HOUR)    # inbox + idle windows pass
    now = w.clock()
    watermark(w, w.store._iso(now - 2 * oversight.PROJECTOR_STALE_S))
    metric(w, DAY, oversight.BURN_LIMIT + 1)
    first = oversight.run_checks(w.conn, now, day=DAY)
    later = oversight.run_checks(w.conn, now + 600, day=DAY)
    assert {f["rule"] for f in first} >= {"R3", "R5", "R6", "R7"}
    assert oversight.digest(first)[0] == oversight.digest(later)[0]


# --------------------------- F3: reconcile alarms keep kind in the finding key
def test_two_reconcile_alarms_with_one_key_but_two_kinds_stay_distinct(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    for alarm_id, kind in ((1, "state_drift"), (2, "lease_gap")):
        w.conn.execute(
            "INSERT INTO projector_alarms(id, kind, workflow_id, key, detail,"
            " raised_at) VALUES(?,?,?,?,?,?)",
            (alarm_id, kind, "wf", "demo:Z", f"{kind} on demo:Z",
             w.store._iso(w.clock())))
    w.conn.commit()
    rows = oversight.r6_projector(w.conn, w.clock())
    alarms = [f for f in rows if f["kind"] == "reconcile_alarm"]
    assert [f["key"] for f in alarms] == ["lease_gap:demo:Z",
                                          "state_drift:demo:Z"]
    assert len({f["reason"] for f in alarms}) == 2
    assert oversight.raise_alarms(w.conn, alarms, w.clock()) == 2
    stored = w.conn.execute(
        "SELECT kind, key FROM projector_alarms WHERE workflow_id='oversight'"
        " AND cleared_at IS NULL ORDER BY key").fetchall()
    assert [(r["kind"], r["key"]) for r in stored] == [
        ("reconcile_alarm", "lease_gap:demo:Z"),
        ("reconcile_alarm", "state_drift:demo:Z")]


# ---------------------------- F4: r5 tolerates a missing questions table
def test_r5_skips_silently_when_the_questions_table_is_missing(w):
    import plangraph.keeper.oversight      # absent -> ModuleNotFoundError (not ImportError)
    from plangraph.keeper import oversight
    seed_needs_you(w)                      # the inbox item is fresh: no findings
    import sqlite3
    raw = sqlite3.connect(str(w.path), isolation_level=None)
    try:
        raw.execute("DROP TABLE questions")
    finally:
        raw.close()
    assert oversight.r5_windows(w.conn, w.clock()) == []
