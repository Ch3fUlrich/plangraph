"""Contract 6: concurrent writers get exactly one winner."""
import threading
from collections import Counter

from contract_world import head, make_plan, need, show, to_in_review, uid, verdict


def _race(jobs):
    """Run callables at the same instant; return their results in order."""
    gate = threading.Barrier(len(jobs))
    out = [None] * len(jobs)

    def run(i, job):
        gate.wait()
        out[i] = job()

    threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return out


def test_concurrent_claim_has_one_winner(kp):
    plan = make_plan(kp)
    who = ["worker1", "worker2", "lead"] * 4
    results = _race([lambda w=w: kp.post("claim", w, {"key": plan.key}) for w in who])
    statuses = Counter(r.status for r in results)
    assert statuses == {200: 1, 409: len(who) - 1}, statuses
    assert all(r.json["error"] == "conflict" for r in results if r.status == 409)
    winner = who[[r.status for r in results].index(200)]
    shown = show(kp, plan.key)
    assert shown["claimed_by"] == winner and shown["epoch"] == 1


def test_concurrent_create_of_one_key_has_one_winner(kp):
    tag = uid()
    body = {"items": [{"project": "demo", "id": "R-" + tag, "kind": "goal", "title": "race " + tag}]}
    results = _race([lambda: kp.post("create_items", "router", body) for _ in range(6)])
    assert Counter(r.status for r in results) == {200: 1, 409: 5}


def test_concurrent_verdicts_make_one_done_transition(kp):
    need(kp, "decisions")
    plan = make_plan(kp)
    to_in_review(kp, plan.key)
    before = head(kp)[0]
    results = _race([lambda w=w: verdict(kp, plan.key, w) for w in ("reviewer1", "reviewer2")])
    statuses = sorted(r.status for r in results)
    assert statuses == [200, 409], [r.raw for r in results]
    assert show(kp, plan.key)["state"] == "done"
    assert len(show(kp, plan.key)["reviews"]) == 1
    assert head(kp)[0] == before + 2           # review_verdict + review_passed, once


def test_concurrent_heartbeats_all_succeed(kp):
    plan = make_plan(kp)
    epoch = kp.post("claim", "worker1", {"key": plan.key}).json["epoch"]
    results = _race([lambda: kp.post("heartbeat", "worker1", {"key": plan.key, "epoch": epoch})
                     for _ in range(8)])
    assert Counter(r.status for r in results) == {200: 8}
