"""Contract 4: evidence, submit and the review (verdict) flows."""

from contract_world import (claim, err, evidence, make_plan, need, ok, show,
                            submit, to_done, to_in_review, uid, verdict)



def test_evidence_verified_and_unverified_rows(kp):
    need(kp, "decisions")
    plan = make_plan(kp)
    epoch = claim(kp, plan.key)
    good = evidence(kp, plan.key, epoch, did="D-1")
    assert good["verified"] is True and good["ref"] == {"id": "D-1"}
    bad = evidence(kp, plan.key, epoch, did="D-9999")
    assert bad["verified"] is False and bad["detail"]
    rows = show(kp, plan.key)["evidence"]
    assert [(r["kind"], r["verified"]) for r in rows] == [("decision", True), ("decision", False)]


def test_evidence_access_rules(kp):
    plan = make_plan(kp)
    key = plan.key
    err(kp.post("add_evidence", "worker1", {"key": key, "epoch": 0, "kind": "decision",
                                            "ref": {"id": "D-1"}}), 400, "bad_state")   # open
    epoch = claim(kp, key)
    body = {"key": key, "epoch": epoch, "kind": "decision", "ref": {"id": "D-1"}}
    err(kp.post("add_evidence", "worker2", body), 403, "forbidden")
    err(kp.post("add_evidence", "reader", body), 403, "forbidden")
    err(kp.post("add_evidence", "worker1", {**body, "kind": "commit", "ref": {}}), 403, "forbidden")
    err(kp.post("add_evidence_router", "worker1", body), 403, "forbidden")
    err(kp.post("add_evidence_router", "reader", body), 403, "forbidden")


def test_router_may_attach_evidence_to_an_active_item(kp):
    need(kp, "decisions")
    plan = make_plan(kp)
    claim(kp, plan.key)
    got = ok(kp.post("add_evidence_router", "router", {
        "key": plan.key, "kind": "decision", "ref": {"id": "D-1"}}), "Evidence")
    assert got["verified"] is True
    err(kp.post("add_evidence_router", "router", {
        "key": make_plan(kp).key, "kind": "decision", "ref": {"id": "D-1"}}), 400, "bad_state")


def test_submit_requires_verified_evidence_of_each_kind(kp):
    need(kp, "decisions")
    plan = make_plan(kp)
    key = plan.key
    epoch = claim(kp, key)
    err(kp.post("submit", "worker1", {"key": key, "epoch": epoch}), 400, "evidence_missing")
    evidence(kp, key, epoch, did="D-9999")
    err(kp.post("submit", "worker1", {"key": key, "epoch": epoch}), 400, "evidence_unverified")
    evidence(kp, key, epoch, did="D-1")
    done = submit(kp, key, epoch)
    assert done == {"key": key, "epoch": epoch, "round": 1, "state": "in_review"}
    shown = show(kp, key)
    assert shown["state"] == "in_review" and shown["lease_until"] is None
    err(kp.post("submit", "worker1", {"key": key, "epoch": epoch}), 400, "bad_state")
    err(kp.post("heartbeat", "worker1", {"key": key, "epoch": epoch}), 400, "bad_state")


def test_submit_by_non_claimant_is_refused(kp):
    need(kp, "decisions")
    plan = make_plan(kp)
    epoch = claim(kp, plan.key)
    evidence(kp, plan.key, epoch)
    err(kp.post("submit", "worker2", {"key": plan.key, "epoch": epoch}), 403, "forbidden")
    err(kp.post("submit", "reader", {"key": plan.key, "epoch": epoch}), 403, "forbidden")


def test_standard_tier_one_cross_family_verdict_makes_done(kp):
    plan = make_plan(kp)
    to_in_review(kp, plan.key)
    out = ok(verdict(kp, plan.key, "reviewer1"), "Verdict")
    assert out == {"key": plan.key, "state": "done", "reviews_passed": 1}
    shown = show(kp, plan.key)
    assert shown["state"] == "done" and shown["reviewer"] == "reviewer1"
    assert shown["reviewed"] is True and shown["done_at"]
    assert shown["reviews"] == [{"reviewer": "reviewer1", "family": "famc"}]


def test_verdict_role_and_family_rules(kp):
    plan = make_plan(kp)
    key = plan.key
    to_in_review(kp, key)
    for who in ("worker1", "worker2", "lead", "router", "reader"):
        err(verdict(kp, key, who), 403, "forbidden")
    err(verdict(kp, key, "reviewer_same"), 409, "same_family")      # writer's family
    err(verdict(kp, key, "reviewer_nofam"), 400, "family_unknown")  # no family at all
    err(kp.post("verdict", "reviewer1", {"key": key}), 400, "validation")   # ok is required
    assert show(kp, key)["state"] == "in_review"


def test_verdict_on_an_item_not_in_review_is_bad_state(kp):
    plan = make_plan(kp)
    err(verdict(kp, plan.key), 400, "bad_state")
    claim(kp, plan.key)
    err(verdict(kp, plan.key), 400, "bad_state")


def test_changes_requested_then_second_consecutive_request_reopens(kp):
    plan = make_plan(kp)
    key = plan.key
    epoch = to_in_review(kp, key)
    out = ok(verdict(kp, key, good=False, reason="rework"), "Verdict")
    assert out == {"key": key, "state": "claimed", "reviews_passed": 0}
    again = show(kp, key)
    assert again["state"] == "claimed" and again["claimed_by"] == "worker1"
    assert again["epoch"] == epoch and again["lease_until"] and again["reviewer"] == "reviewer1"
    assert submit(kp, key, epoch)["round"] == 2
    out = ok(verdict(kp, key, good=False, reason="still wrong"), "Verdict")
    assert out["state"] == "open"
    reopened = show(kp, key)
    assert reopened["state"] == "open" and reopened["claimed_by"] is None
    findings = ok(kp.get("/v1/alerts?fmt=json", "reader"), "Alerts")["findings"]
    assert any(f["key"] == key and f["code"] == "changes_requested_twice" for f in findings)


def test_high_tier_needs_two_distinct_principals_of_distinct_families(kp):
    plan = make_plan(kp, review_tier="high")
    key = plan.key
    to_in_review(kp, key)
    first = ok(verdict(kp, key, "reviewer1"), "Verdict")
    assert first == {"key": key, "state": "in_review", "reviews_passed": 1}
    err(verdict(kp, key, "reviewer1"), 409, "same_principal")
    err(verdict(kp, key, "reviewer_same"), 409, "same_family")
    last = ok(verdict(kp, key, "reviewer2"), "Verdict")
    assert last == {"key": key, "state": "done", "reviews_passed": 2}
    assert [r["reviewer"] for r in show(kp, key)["reviews"]] == ["reviewer1", "reviewer2"]
    err(verdict(kp, key, "reviewer2"), 409, "locked")


def test_high_tier_second_reviewer_in_the_first_family_is_refused(kp):
    need(kp, "admin")
    name = "rev-" + uid().lower()
    out = kp.admin("add-principal", name, "--role", "reviewer", "--projects", "demo", "--family", "famC")
    assert out.returncode == 0, out.stderr
    kp.tokens[name] = out.stdout.strip().rsplit(":", 1)[1].strip()
    plan = make_plan(kp, review_tier="high")
    to_in_review(kp, plan.key)
    ok(verdict(kp, plan.key, "reviewer1"), "Verdict")
    err(verdict(kp, plan.key, name), 409, "same_family")


def test_done_item_cannot_be_reviewed_again(kp):
    plan = make_plan(kp)
    to_done(kp, plan.key)
    err(verdict(kp, plan.key, "reviewer2"), 409, "locked")
