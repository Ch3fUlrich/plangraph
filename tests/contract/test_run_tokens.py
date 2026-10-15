"""Contract 11: run records, run tokens, offline replay and the reaper.

Run tokens need the ``runs`` capability: a run directory ``<runs>/<run_id>/job.json``
whose ``request.title`` mentions the item key.
"""
import json
import uuid

from contract_world import claim, err, make_plan, need, ok, show, to_in_review, uid


def _run_dir(kp, key, family=None):
    need(kp, "runs")
    run_id = "run-" + uid().lower()
    (kp.runs_dir / run_id).mkdir(parents=True)
    request = {"title": f"work on {key}"}
    if family:
        request["family"] = family
    (kp.runs_dir / run_id / "job.json").write_text(json.dumps({"request": request}), encoding="utf-8")
    return run_id


def _mint(kp, key, role, run_id, who="router"):
    return kp.post("mint_run_token", who, {"key": key, "role": role, "run_id": run_id})


def test_record_run_is_spawner_only_and_idempotent(kp):
    plan = make_plan(kp)
    key = plan.key
    run = {"run_id": "r-" + uid().lower(), "phase": "started",
           "writer": {"model": "model-a", "family": "famb"}}
    err(kp.post("record_run", "spawner", {"key": key, "run": run}), 400, "bad_state")   # not active
    claim(kp, key)
    for who in ("router", "lead", "worker1", "reader"):
        err(kp.post("record_run", who, {"key": key, "run": run}), 403, "forbidden")
    out = ok(kp.post("record_run", "spawner", {"key": key, "run": run}), "RecordRun")
    assert out["run_id"] == run["run_id"] and out["run_ids"] == [run["run_id"]]
    again = ok(kp.post("record_run", "spawner", {"key": key, "run": run}), "RecordRun")
    assert again["duplicate"] is True
    done = {**run, "phase": "finished", "rc": 0, "seats": [{"model": "model-b", "verdict": "pass"}]}
    ok(kp.post("record_run", "spawner", {"key": key, "run": done}), "RecordRun")
    row = show(kp, key)
    assert row["run_ids"] == [run["run_id"]] and row["writer_model"] == "model-a"
    assert row["seat_models"] == [{"model": "model-b", "verdict": "pass"}]


def test_record_run_validation(kp):
    plan = make_plan(kp)
    claim(kp, plan.key)
    for run in ({"phase": "started"}, {"run_id": "bad id!", "phase": "started"},
                {"run_id": "r1", "phase": "paused"}, {"run_id": "r1", "phase": "started", "extra": 1},
                "text"):
        err(kp.post("record_run", "spawner", {"key": plan.key, "run": run}), 400, "validation")


def test_writer_run_token_lifecycle(kp):
    plan = make_plan(kp, 2)
    key, other = plan.lanes
    epoch = claim(kp, key)
    run_id = _run_dir(kp, key)
    for who in ("worker1", "reader", "reviewer1", "lead_other"):
        err(_mint(kp, key, "writer", run_id, who), 403, "forbidden")
    err(_mint(kp, key, "reviewer", run_id), 400, "bad_state")            # reviewer needs in_review
    err(_mint(kp, key, "writer", "run-missing"), 400, "evidence_unverified")
    minted = ok(_mint(kp, key, "writer", run_id), "MintToken")
    assert minted["token"].startswith("pgr1_") and minted["epoch"] == epoch
    token = minted["token"]
    assert show(kp, key)["epoch"] == epoch
    # the token may only call its own verbs, on its own item
    err(kp.get("/v1/whoami", token=token), 403, "forbidden")
    err(kp.get("/v1/ready", token=token), 403, "forbidden")
    err(kp.post("claim", None, {"key": other}, token=token), 403, "forbidden")
    err(kp.post("heartbeat", None, {"key": other, "epoch": 0}, token=token), 403, "forbidden")
    err(kp.post("verdict", None, {"key": key, "ok": True}, token=token), 403, "forbidden")
    ok(kp.post("heartbeat", None, {"key": key, "epoch": epoch}, token=token), "Claim")
    need(kp, "decisions")
    got = ok(kp.post("add_evidence", None, {"key": key, "epoch": epoch, "kind": "decision",
                                            "ref": {"id": "D-1"}}, token=token), "Evidence")
    assert got["verified"] is True
    ok(kp.post("submit", None, {"key": key, "epoch": epoch}, token=token), "Submit")
    # submit revokes the writer token
    err(kp.post("heartbeat", None, {"key": key, "epoch": epoch}, token=token), 401, "forbidden")


def test_reviewer_run_token_carries_the_run_family(kp):
    plan = make_plan(kp)
    key = plan.key
    to_in_review(kp, key)
    run_id = _run_dir(kp, key, family="famc")
    token = ok(_mint(kp, key, "reviewer", run_id), "MintToken")["token"]
    err(kp.post("heartbeat", None, {"key": key, "epoch": 1}, token=token), 403, "forbidden")
    out = ok(kp.post("verdict", None, {"key": key, "ok": True, "reason": "ok"}, token=token), "Verdict")
    assert out["state"] == "done"
    assert show(kp, key)["reviews"][0]["family"] == "famc"


def test_replay_requires_a_run_token_and_is_idempotent(kp):
    plan = make_plan(kp)
    key = plan.key
    epoch = claim(kp, key)
    err(kp.post("replay", "router", {"entries": []}), 403, "forbidden")
    err(kp.post("replay", "worker1", {"entries": [{"id": "0" * 32, "verb": "heartbeat", "body": {}}]}),
        403, "forbidden")
    token = ok(_mint(kp, key, "writer", _run_dir(kp, key)), "MintToken")["token"]
    err(kp.post("replay", None, {"entries": []}, token=token), 400, "validation")
    entry_id = uuid.uuid4().hex
    entries = [{"id": entry_id, "verb": "heartbeat", "body": {"key": key, "epoch": epoch}},
               {"id": uuid.uuid4().hex, "verb": "claim", "body": {"key": key}},
               {"id": "not-an-id", "verb": "heartbeat", "body": {}}]
    out = ok(kp.post("replay", None, {"entries": entries}, token=token), "Replay")
    assert [r["result"] for r in out["results"]] == ["ok", "forbidden", "validation"]
    again = ok(kp.post("replay", None, {"entries": entries[:1]}, token=token), "Replay")
    assert again["results"] == [{"id": entry_id, "result": "duplicate", "first": "ok"}]


def test_replayed_verdict_ok_must_be_a_json_boolean(kp):
    plan = make_plan(kp)
    key = plan.key
    to_in_review(kp, key)
    token = ok(_mint(kp, key, "reviewer", _run_dir(kp, key, family="famc")), "MintToken")["token"]
    entry = lambda value: {"id": uuid.uuid4().hex, "verb": "verdict",       # noqa: E731
                           "body": {"key": key, "ok": value, "reason": "r"}}
    out = ok(kp.post("replay", None, {"entries": [entry("false")]}, token=token), "Replay")
    assert [r["result"] for r in out["results"]] == ["validation"]
    assert show(kp, key)["state"] == "in_review"
    out = ok(kp.post("replay", None, {"entries": [entry(True)]}, token=token), "Replay")
    assert [r["result"] for r in out["results"]] == ["ok"]
    assert show(kp, key)["state"] == "done"


def test_run_token_is_dead_after_the_item_moves_on(kp):
    plan = make_plan(kp)
    key = plan.key
    epoch = claim(kp, key)
    token = ok(_mint(kp, key, "writer", _run_dir(kp, key)), "MintToken")["token"]
    ok(kp.post("release", "worker1", {"key": key, "epoch": epoch}), "KeyOnly")
    err(kp.post("heartbeat", None, {"key": key, "epoch": epoch}, token=token), 401, "forbidden")
    claim(kp, key, "worker2")                                          # new epoch: old token stays dead
    err(kp.post("heartbeat", None, {"key": key, "epoch": epoch + 1}, token=token), 401, "forbidden")


def test_reap_is_system_only(kp):
    for who in ("router", "lead", "worker1", "reader", "spawner"):
        err(kp.post("reap", who, {}), 403, "forbidden")
