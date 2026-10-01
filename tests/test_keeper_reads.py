"""Read verbs: ready, show, blocked, waves, critical_path, percent, alerts,
digest, snapshot, whoami — all built on plangraph.views."""
import pytest

from keeper_util import World, seed, to_claimed, to_done, to_in_review
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", ["A"])])
    yield world
    world.close()


def test_ready_returns_views_brief_text(w):
    text = w.store.ready(w.p("reader"))
    assert "demo:A" in text and "demo:B" not in text


def test_ready_project_filter(w):
    assert w.store.ready(w.p("reader"), project="nope") == ""


def test_show_compact(w):
    key = "demo:A"
    epoch = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    info = w.store.show(w.p("reader"), key)
    assert info["key"] == key and info["state"] == "claimed"
    assert info["epoch"] == epoch
    assert info["lease_until"] and info["claimed_by"] == "l1main"
    assert "demo:B" in info["unblocks"]
    assert info["evidence"][0]["kind"] == "decision"
    assert info["depends_on"] == []


def test_blocked(w):
    assert w.store.blocked(w.p("reader")) == {
        "demo:B": ["demo:A"], "demo:GOAL": ["demo:A", "demo:B"]}


def test_waves(w):
    assert w.store.waves(w.p("reader")) == [["demo:A"], ["demo:B"], ["demo:GOAL"]]


def test_critical_path(w):
    path = w.store.critical_path(w.p("reader"))
    assert path["keys"][0] == "demo:A"
    assert path["hours"] >= 2


def test_percent(w):
    value = w.store.percent(w.p("reader"), by="count")
    assert 0.0 <= value <= 100.0


def test_whoami(w):
    who = w.store.whoami(w.p("l1main"))
    assert who == {"name": "l1main", "role": "l1", "projects": ["demo"],
                   "family": "teama"}


def test_snapshot_head_and_fields(w):
    snap = w.store.snapshot(w.p("reader"))
    head = w.conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    assert snap["head"] == (head[0] if head else None)
    item = {it["key"]: it for it in snap["items"]}["demo:A"]
    assert {"epoch", "evidence", "updated_at", "corrected_by"} <= set(item)


def test_snapshot_annotates_corrected_by(w):
    to_done(w, "demo:A")
    items = [{"project": "demo", "id": "FIX", "title": "fix", "kind": "lane",
              "state": "open", "depends_on": [], "corrects": "demo:A",
              "evidence_kinds": ["any"]},
             {"project": "demo", "id": "GOAL2", "kind": "goal", "state": "open",
              "depends_on": ["demo:FIX"], "evidence_kinds": ["any"]}]
    w.store.create_items(w.p("router"), items)
    snap = w.store.snapshot(w.p("reader"))
    a = {it["key"]: it for it in snap["items"]}["demo:A"]
    assert a["corrected_by"] == ["demo:FIX"]


# ------------------------------------------------------------------ alerts

def test_alerts_flags_expiring_lease(w):
    to_claimed(w, "demo:A", lease_s=600)
    alerts = w.store.alerts(w.p("reader"))
    assert [x["key"] for x in alerts["lease_expiring"]] == ["demo:A"]


def test_alerts_flags_stall_after_cancel(w):
    w.store.cancel(w.p("router"), "demo:A", "dropped")
    alerts = w.store.alerts(w.p("reader"))
    assert alerts["stalled"] is True


def test_alerts_flags_repeated_refusals(w):
    for _ in range(2):
        with pytest.raises(Refused):
            w.store.claim(w.p("reader"), "demo:A")
    alerts = w.store.alerts(w.p("reader"))
    refusals = {x["key"]: x["count"] for x in alerts["refusals"]}
    assert refusals.get("demo:A", 0) >= 2


# ------------------------------------------------------------------ digest

def test_digest_router_only(w):
    with pytest.raises(Refused) as exc:
        w.store.digest(w.p("reader"))
    assert exc.value.code == "forbidden"


def test_digest_counts_and_done(w):
    to_done(w, "demo:A")
    digest = w.store.digest(w.p("router"))
    assert digest["projects"]["demo"]["done"] >= 1
    assert "demo:A" in [d["key"] for d in digest["done"]]


# ------------------------------------------------------------------- audit

def test_audit_records_success_and_refusal(w):
    to_claimed(w, "demo:A")
    with pytest.raises(Refused):
        w.store.claim(w.p("reader"), "demo:A")
    rows = [tuple(r) for r in w.conn.execute(
        "SELECT action, ok, code FROM audit_log ORDER BY id").fetchall()]
    assert ("claim", 1, None) in rows
    assert any(action == "claim" and ok == 0 and code == "forbidden"
               for action, ok, code in rows)
