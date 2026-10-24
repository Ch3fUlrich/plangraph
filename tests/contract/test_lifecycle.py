"""Contract 3: claim, heartbeat, release, delegate, cancel and the leases."""
from datetime import datetime

from contract_world import claim, err, make_plan, ok, show, snapshot, uid


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_claim_then_release_returns_the_item_to_open(kp):
    plan = make_plan(kp)
    key = plan.key
    got = ok(kp.post("claim", "worker1", {"key": key}), "Claim")
    assert got["key"] == key and got["epoch"] == 1
    held = show(kp, key)
    assert held["state"] == "claimed" and held["claimed_by"] == "worker1"
    assert held["lease_until"] == got["lease_until"] and held["epoch"] == 1
    assert key not in [r["key"] for r in ok(kp.get("/v1/ready?all=1&fmt=json", "reader"))["items"]]
    ok(kp.post("release", "worker1", {"key": key, "epoch": 1}), "KeyOnly")
    back = show(kp, key)
    assert back["state"] == "open" and back["claimed_by"] is None and back["lease_until"] is None
    assert claim(kp, key, "worker2") == 2          # epoch only ever grows


def test_lease_is_clamped_to_60s_through_one_day(kp):
    plan = make_plan(kp, 2)
    for key, asked, want in ((plan.lanes[0], 5, 60), (plan.lanes[1], 10 ** 7, 86400)):
        got = ok(kp.post("claim", "worker1", {"key": key, "lease_s": asked}), "Claim")
        now = _ts(snapshot(kp)["generated"])
        left = (_ts(got["lease_until"]) - now).total_seconds()
        assert abs(left - want) <= 5, (asked, left)


def test_heartbeat_extends_and_never_shortens_the_lease(kp):
    plan = make_plan(kp)
    got = ok(kp.post("claim", "worker1", {"key": plan.key, "lease_s": 86400}), "Claim")
    beat = ok(kp.post("heartbeat", "worker1", {"key": plan.key, "epoch": got["epoch"]}), "Claim")
    assert beat["epoch"] == got["epoch"]
    assert _ts(beat["lease_until"]) >= _ts(got["lease_until"])
    assert show(kp, plan.key)["lease_until"] == beat["lease_until"]


def test_claim_refusals(kp):
    plan = make_plan(kp, 2, chain=True)
    first, second = plan.lanes
    err(kp.post("claim", "worker1", {"key": second}), 400, "bad_state")       # unmet dependency
    err(kp.post("claim", "reader", {"key": first}), 403, "forbidden")
    err(kp.post("claim", "router", {"key": first}), 403, "forbidden")
    err(kp.post("claim", "reviewer1", {"key": first}), 403, "forbidden")
    err(kp.post("claim", "lead_other", {"key": first}), 403, "forbidden")     # other project
    claim(kp, first, "lead")                                                  # an l1 may claim
    err(kp.post("claim", "worker1", {"key": first}), 409, "conflict")


def test_only_the_claimant_or_delegate_may_act(kp):
    plan = make_plan(kp)
    key = plan.key
    epoch = claim(kp, key, "worker1")
    err(kp.post("heartbeat", "worker2", {"key": key, "epoch": epoch}), 403, "forbidden")
    err(kp.post("release", "worker2", {"key": key, "epoch": epoch}), 403, "forbidden")
    err(kp.post("delegate", "worker2", {"key": key, "worker": "worker2"}), 403, "forbidden")
    err(kp.post("delegate", "worker1", {"key": key, "worker": "reviewer1"}), 400, "validation")
    assert ok(kp.post("delegate", "worker1", {"key": key, "worker": "worker2"}), "Delegate")["delegate"] == "worker2"
    ok(kp.post("heartbeat", "worker2", {"key": key, "epoch": epoch}), "Claim")     # delegate may
    assert show(kp, key)["delegate"] == "worker2"


def test_router_may_release_someone_elses_claim(kp):
    plan = make_plan(kp)
    epoch = claim(kp, plan.key)
    ok(kp.post("release", "router", {"key": plan.key, "epoch": epoch}), "KeyOnly")
    assert show(kp, plan.key)["state"] == "open"


def test_stale_epoch_is_refused_everywhere(kp):
    plan = make_plan(kp)
    key = plan.key
    first = claim(kp, key)
    ok(kp.post("release", "worker1", {"key": key, "epoch": first}), "KeyOnly")
    second = claim(kp, key, "worker2")
    assert second == first + 1
    for verb in ("heartbeat", "release", "submit"):
        err(kp.post(verb, "worker2", {"key": key, "epoch": first}), 409, "epoch_stale")
    err(kp.post("add_evidence", "worker2", {"key": key, "epoch": first, "kind": "decision",
                                            "ref": {"id": "D-1"}}), 409, "epoch_stale")


def test_state_gates(kp):
    plan = make_plan(kp)
    key = plan.key
    err(kp.post("heartbeat", "worker1", {"key": key, "epoch": 0}), 400, "bad_state")   # open
    err(kp.post("release", "worker1", {"key": key, "epoch": 0}), 400, "bad_state")
    err(kp.post("submit", "worker1", {"key": key, "epoch": 0}), 400, "bad_state")
    err(kp.post("delegate", "worker1", {"key": key, "worker": "worker2"}), 400, "bad_state")


def test_cancel_open_claimed_and_role_scope(kp):
    plan = make_plan(kp, 3)
    a, b, c = plan.lanes
    err(kp.post("cancel", "worker1", {"key": a}), 403, "forbidden")
    err(kp.post("cancel", "lead_other", {"key": a}), 403, "forbidden")
    err(kp.post("cancel", "reader", {"key": a}), 403, "forbidden")
    claim(kp, b)
    assert ok(kp.post("cancel", "router", {"key": a, "reason": "no longer needed"}), "Cancel")["state"] == "cancelled"
    assert ok(kp.post("cancel", "router", {"key": b}), "Cancel")["state"] == "cancelled"
    assert show(kp, a)["state"] == "cancelled" and show(kp, b)["state"] == "cancelled"


def test_cancelling_a_dependency_is_refused_for_l1_with_active_dependents(kp):
    plan = make_plan(kp, 2, chain=True)
    first, second = plan.lanes
    err(kp.post("cancel", "lead", {"key": first}), 409, "conflict")
    assert show(kp, first)["state"] == "open"


def test_unknown_project_scope_for_lead_creation(kp):
    tag = uid()
    body = {"items": [{"project": "other", "id": "X-" + tag, "kind": "goal", "title": "x " + tag}]}
    err(kp.post("create_items", "lead", body), 403, "forbidden")
    ok(kp.post("create_items", "lead_other", body), "Created")
