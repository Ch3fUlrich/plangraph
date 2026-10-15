"""Contract 2: read routes (shapes, filters, role gates)."""
from contract_world import err, make_plan, need, ok, show, to_done, uid


def _keys(rows):
    return [row["key"] for row in rows]


def test_ready_lists_only_unblocked_open_items(kp):
    plan = make_plan(kp, 2, chain=True)
    first, second = plan.lanes
    body = ok(kp.get("/v1/ready?all=1&fmt=json", "reader"), "Ready")
    assert first in _keys(body["items"]) and second not in _keys(body["items"])
    assert plan.goal not in _keys(body["items"])
    assert body["unblocks"][first] >= 1


def test_ready_filters_and_limit(kp):
    plan = make_plan(kp, 3, project="demo", owner="someone")
    body = ok(kp.get("/v1/ready?all=1&fmt=json&project=demo", "reader"), "Ready")
    assert set(plan.lanes) <= set(_keys(body["items"]))
    assert all(row["project"] == "demo" for row in body["items"])
    assert ok(kp.get("/v1/ready?fmt=json&project=none-such", "reader"), "Ready")["items"] == []
    limited = ok(kp.get("/v1/ready?n=1&fmt=json", "reader"), "Ready")
    assert len(limited["items"]) == 1
    owned = ok(kp.get("/v1/ready?all=1&fmt=json&owner=someone", "reader"), "Ready")
    assert set(plan.lanes) <= set(_keys(owned["items"]))
    assert all(row["owner"] == "someone" for row in owned["items"])
    assert set(plan.lanes).isdisjoint(_keys(
        ok(kp.get("/v1/ready?all=1&fmt=json&mine=1", "reader"), "Ready")["items"]))


def test_ready_orders_by_priority_first(kp):
    plan = make_plan(kp, 2)
    low, high = plan.lanes
    ok(kp.post("set_priority", "router", {"key": high, "n": 10 ** 6}), "Priority")
    items = ok(kp.get("/v1/ready?all=1&fmt=json", "reader"), "Ready")["items"]
    order = _keys(items)
    assert order.index(high) < order.index(low)
    assert order[0] == high or items[0]["priority"] >= 10 ** 6


def test_ready_agent_flag_drops_operator_items(kp):
    plan = make_plan(kp, 1)
    op_tag = uid()
    ok(kp.post("create_items", "router", {"items": [{
        "project": "demo", "id": "OP-" + op_tag, "kind": "question", "title": "ask " + op_tag,
        "depends_on": [], "evidence_kinds": ["decision"]}, {
        "project": "demo", "id": "G2-" + op_tag, "kind": "goal", "title": "g " + op_tag,
        "depends_on": ["OP-" + op_tag]}]}), "Created")
    everything = _keys(ok(kp.get("/v1/ready?all=1&fmt=json", "reader"), "Ready")["items"])
    agent = _keys(ok(kp.get("/v1/ready?all=1&fmt=json&agent=1", "reader"), "Ready")["items"])
    assert f"demo:OP-{op_tag}" in everything and f"demo:OP-{op_tag}" not in agent
    assert plan.key in agent


def test_show_json_text_brief_and_path_form(kp):
    plan = make_plan(kp, 2, chain=True)
    first, second = plan.lanes
    body = show(kp, second)
    assert body["state"] == "open" and body["epoch"] == 0
    assert body["depends_on"] == [{"key": first, "state": "open"}]
    assert set(show(kp, first)["unblocks"]) == {second, plan.goal}
    by_path = ok(kp.get(f"/v1/show/{first}?fmt=json", "reader"), "Show")
    assert by_path["key"] == first
    brief = ok(kp.get(f"/v1/show?key={second}&fmt=json&brief=1", "reader"), "Brief")
    assert brief["key"] == second and brief["depends_on"][0]["key"] == first
    assert "text/plain" in kp.get(f"/v1/show?key={first}", "reader").ctype


def test_blocked_waves_critical_path_percent(kp):
    plan = make_plan(kp, 2, chain=True, estimate_h=2)
    first, second = plan.lanes
    blocked = ok(kp.get("/v1/blocked?fmt=json", "reader"), "Blocked")
    assert blocked[second] == [first] and first not in blocked
    assert blocked[plan.goal] == sorted([first, second])
    only_demo = ok(kp.get("/v1/blocked?fmt=json&project=other", "reader"), "Blocked")
    assert second not in only_demo
    waves = ok(kp.get("/v1/waves", "reader"), "Waves")
    where = {key: i for i, layer in enumerate(waves) for key in layer}
    assert where[first] < where[second] < where[plan.goal]
    cp = ok(kp.get("/v1/critical_path", "reader"), "CriticalPath")
    assert cp["hours"] >= 4.0 and cp["keys"][-1]
    for by in ("estimate", "count"):
        pct = ok(kp.get(f"/v1/percent?by={by}", "reader"), "Percent")
        assert 0.0 <= pct <= 100.0


def test_alerts_digest_and_role_gate(kp):
    make_plan(kp)
    ok(kp.get("/v1/alerts?fmt=json", "reader"), "Alerts")
    digest = ok(kp.get("/v1/digest", "router"), "Digest")
    assert digest["projects"]["demo"]["open"] >= 1
    for who in ("reader", "lead", "worker1", "reviewer1"):
        err(kp.get("/v1/digest", who), 403, "forbidden")


def test_audit_meta_is_router_and_l1_only(kp):
    make_plan(kp)
    ok(kp.get("/v1/audit-meta?fmt=json", "router"), "AuditMeta")
    ok(kp.get("/v1/audit-meta?fmt=json&project=demo", "lead"), "AuditMeta")
    err(kp.get("/v1/audit-meta?fmt=json&project=demo", "lead_other"), 403, "forbidden")
    err(kp.get("/v1/audit-meta?fmt=json", "worker1"), 403, "forbidden")
    err(kp.get("/v1/audit-meta?fmt=json&stale_days=zero", "router"), 400, "validation")


def test_needs_you_answers_progress_aliases(kp):
    tag = uid()
    ok(kp.post("create_items", "router", {"items": [
        {"project": "demo", "id": "Q-" + tag, "kind": "question", "title": "ask " + tag,
         "depends_on": []},
        {"project": "demo", "id": "G-" + tag, "kind": "goal", "title": "g " + tag,
         "depends_on": ["Q-" + tag]}]}), "Created")
    rows = ok(kp.get("/v1/needs_you?fmt=json", "reader"), "NeedsYou")
    assert f"demo:Q-{tag}" in _keys(rows)
    ok(kp.get("/v1/answers?fmt=json", "reader"), "Answers")
    prog = ok(kp.get("/v1/progress", "reader"), "Progress")
    assert any(p["project"] == "demo" for p in prog["projects"])
    ok(kp.get("/v1/aliases?fmt=json", "reader"), "Aliases")


def test_snapshot_shape_and_consistency(kp):
    plan = make_plan(kp, 2)
    snap = ok(kp.get("/v1/snapshot", "reader"), "Snapshot")
    index = {item["key"]: item for item in snap["items"]}
    assert set(plan.lanes) | {plan.goal} <= set(index)
    assert sorted(index[plan.goal]["depends_on"]) == sorted(plan.lanes)
    assert snap["head_seq"] >= 1 and len(snap["head"]) == 64
    shown = show(kp, plan.key)
    row = index[plan.key]
    for field in ("state", "epoch", "priority", "review_tier", "title"):
        assert row[field] == shown[field]


def test_changelog_requires_since_and_lists_creations(kp):
    plan = make_plan(kp)
    err(kp.get("/v1/changelog?fmt=json", "reader"), 400, "validation")
    err(kp.get("/v1/changelog?fmt=json&since=banana", "reader"), 400, "validation")
    body = ok(kp.get("/v1/changelog?fmt=json&since=0", "reader"), "Changelog")
    created = [e["key"] for e in body["entries"] if e["type"] == "created"]
    assert plan.key in created and plan.goal in created
    assert body["head_seq"] >= max(e["seq"] for e in body["entries"])
    later = ok(kp.get(f"/v1/changelog?fmt=json&since={body['head_seq']}", "reader"), "Changelog")
    assert later["entries"] == []


def test_done_items_appear_in_snapshot_with_done_fields(kp):
    need(kp, "decisions")
    plan = make_plan(kp)
    to_done(kp, plan.key)
    row = {i["key"]: i for i in ok(kp.get("/v1/snapshot", "reader"), "Snapshot")["items"]}[plan.key]
    assert row["state"] == "done" and row["reviewed"] is True and row["done_at"]
    assert row["claimed_by"] == "worker1"
    err(kp.post("claim", "worker1", {"key": plan.key}), 409, "locked")
