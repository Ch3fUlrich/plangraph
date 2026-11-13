"""History/apply establishes execution ownership on SQLite and PostgreSQL."""
import base64
import json

import pytest

from keeper_util import seed, to_claimed
from plangraph.keeper.errors import Refused
from plangraph.projector.apply import Projector
from plangraph.projector.history import project_history
from test_keeper_exec_projector_path import event, history
from test_keeper_worker_principal import scoped


@pytest.fixture
def world(scoped):
    w, _ = scoped
    seed(w, [("A", [])])
    return w


def identity(run="R2", attempt=1):
    return {"workflow_id": "card:demo:A", "run_id": run, "attempt": attempt}


def payload(body):
    return {"payloads": [{"metadata": {"encoding": "anNvbi9wbGFpbg=="},
                          "data": base64.b64encode(json.dumps(body).encode()).decode()}]}


def project(w, run="R2", *, ordinal=1, attempt=1):
    h = history("needs_input")
    h["events"][1]["activityTaskScheduledEventAttributes"]["input"] = payload(
        {"n": attempt, **identity(run, attempt)})
    p = project_history(h, workflow_id="card:demo:A", run_id=run)
    return Projector(w.store, w.p("system"), now=w.clock).apply_run(p, run_ord=ordinal)


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("verb", ["park", "alarm", "ask_question", "attempt"])
def test_r2_projection_fences_r1_before_any_worker_fact(world, claimed, verb):
    w, key = world, "demo:A"
    if claimed:
        to_claimed(w, key, "workerA")
    project(w)
    before = dict(w.store._item(key))
    def call(run):
        body = {"card": key, **identity(run)}
        if verb == "ask_question":
            return w.store.ask_question(w.p("workerA"), key, "Which?",
                **{"exec_" + k: v for k, v in identity(run).items()})
        if verb == "park":
            body.update(exit_class="needs_input", expected_state=None if claimed else "needs_input")
        elif verb == "alarm":
            body.update(reason="question")
        else:
            body.update(n=1, exit_class="ok")
        return getattr(w.store, "worker_" + verb)(w.p("workerA"), body)
    with pytest.raises(Refused) as exc:
        call("R1")
    assert exc.value.code == "exec_stale"
    assert dict(w.store._item(key)) == before
    call("R2")
    assert w.store._item(key)["exec_run_id"] == "R2"
    verbs = [r[0] for r in w.conn.execute(
        "SELECT action FROM audit_log WHERE actor='system' AND key=?", (key,))]
    assert verbs.count("project_state") == 2 and "set_state" not in verbs
    assert w.store.verify_integrity() == []


def test_projection_records_workflow_attempt_not_activity_retry(world):
    h = history("needs_input")
    h["events"][1]["activityTaskScheduledEventAttributes"]["input"] = payload({"n": 3})
    h["events"][2]["activityTaskStartedEventAttributes"]["attempt"] = 7
    p = project_history(h, workflow_id="card:demo:A", run_id="R2")
    Projector(world.store, world.p("system")).apply_run(p, run_ord=1)
    assert (world.store._item("demo:A")["exec_attempt"], p.attempts[0].attempt) == (3, 7)
    with pytest.raises(Refused) as exc:
        world.store.worker_alarm(world.p("workerA"), {
            "card": "demo:A", "reason": "late", **identity(attempt=2)})
    assert exc.value.code == "exec_stale"


def test_newer_projected_run_retires_old_owner_and_old_projection_is_stale(world):
    project(world, "R1", ordinal=0)
    project(world)
    assert world.store._item("demo:A")["exec_run_id"] == "R2"
    assert project(world, "R0", ordinal=0).states_stale == 2
    with pytest.raises(Refused) as exc:
        world.store.worker_alarm(world.p("workerA"), {
            "card": "demo:A", "reason": "late", **identity("R1")})
    assert exc.value.code == "exec_stale"
    world.store.worker_alarm(world.p("workerA"), {
        "card": "demo:A", "reason": "live", **identity()})
    assert world.store.verify_integrity() == []


def test_second_scheduled_attempt_advances_owner_even_while_running(world):
    h = history("ok")
    h["events"][-1] = event(5, "ACTIVITY_TASK_SCHEDULED", activityId="attempt-2",
        activityType={"name": "attempt"}, input=payload({"n": 2}))
    p = project_history(h, workflow_id="card:demo:A", run_id="R2")
    Projector(world.store, world.p("system")).apply_run(p, run_ord=1)
    assert world.store._item("demo:A")["exec_attempt"] == 2


@pytest.mark.parametrize("body", [{"n": 0}, {"n": True}, {"n": 2**31}, {"run_id": "wrong"}])
def test_invalid_history_identity_does_not_install_owner(world, body):
    h = {"events": history("ok")["events"][:3]}
    h["events"][1]["activityTaskScheduledEventAttributes"]["input"] = payload(body)
    p = project_history(h, workflow_id="card:demo:A", run_id="R2")
    assert len(p.shape_issues) == 1 and p.states == ()
    Projector(world.store, world.p("system")).apply_run(p, run_ord=1)
    assert world.store._item("demo:A")["exec_run_id"] is None


def test_projected_supersession_keeps_claim_and_principal_consistent(world):
    to_claimed(world, "demo:A")
    world.store.delegate(world.p("l1main"), "demo:A", "workerA")
    project(world, "R1", ordinal=0)
    before = dict(world.store._item("demo:A"))
    project(world)
    item = world.store._item("demo:A")
    assert (item["exec_run_id"], item["exec_principal"]) == ("R2", "workerA")
    assert all(item[k] == before[k] for k in ("state", "claimed_by", "delegate", "epoch", "lease_until"))
    world.store.worker_alarm(world.p("workerA"), {
        "card": "demo:A", "reason": "live", **identity()})
    assert world.store.verify_integrity() == []


@pytest.mark.parametrize("target", ["claimed", "done", "cancelled", "in_review", "invalid"])
def test_nonsettable_projection_is_skipped_without_validation_outage(world, target):
    from dataclasses import replace
    p = project_history(history("ok"), workflow_id="card:demo:A", run_id="R2")
    p = replace(p, states=(replace(p.states[0], state=target),))
    before = dict(world.store._item("demo:A"))
    result = Projector(world.store, world.p("system")).apply_run(p, run_ord=1)
    assert (result.states_skipped_state, result.states_written) == (1, 0)
    assert dict(world.store._item("demo:A")) == before


@pytest.mark.parametrize("principal", ["router", "l1main", "workerA"])
def test_projection_has_distinct_audit_verb(world, principal):
    p = project_history({"events": history("ok")["events"][:3]},
                        workflow_id="card:demo:A", run_id="R2")
    before = dict(world.store._item("demo:A"))
    with pytest.raises(Refused) as exc:
        Projector(world.store, world.p(principal)).apply_run(p, run_ord=1)
    assert exc.value.code == "forbidden"
    assert dict(world.store._item("demo:A")) == before
    assert world.conn.execute(
        "SELECT 1 FROM audit_log WHERE action='project_state' AND ok=0").fetchone() is not None


@pytest.mark.parametrize("old_watermark", [False, True])
def test_refused_r3_retries_after_worker_recorded_r2_is_projected(world, old_watermark):
    from plangraph.projector.reconcile import open_alarms, reconcile
    from test_projector_apply import FakeSource
    w, key = world, "demo:A"
    if old_watermark:
        project(w, "R0", ordinal=0)
        w.store.set_state(w.p("router"), key, "open", hand_back=True)
    to_claimed(w, key, "workerA")
    w.store.worker_park(w.p("workerA"), {
        "card": key, "exit_class": "needs_input", **identity()})
    before = dict(w.store._item(key))
    watermark = list(w.conn.execute("SELECT * FROM projector_watermark"))
    assert before["claimed_by"] is None and before["delegate"] is None
    for _ in range(2):  # New Projector instance per pass: alarms must survive restarts.
        w.clock.advance(1)
        assert project(w, "R3", ordinal=2).states_skipped_state == 2
        assert dict(w.store._item(key)) == before
        assert list(w.conn.execute("SELECT * FROM projector_watermark")) == watermark
        assert w.conn.execute(
            "SELECT 1 FROM projector_applied WHERE run_id='R3' AND kind='state'").fetchone() is None
        alarms = open_alarms(w.store)
        assert len(alarms) == 2 and all(a["kind"] == "exec_stale" for a in alarms)
        assert all(a["raised_at"] == w.store._iso(w.clock()) for a in alarms)
    reconcile(w.store, FakeSource([]), now=w.clock)
    assert open_alarms(w.store) == alarms
    project(w, "R2", ordinal=1)
    assert project(w, "R3", ordinal=2).states_written == 2
    assert open_alarms(w.store) == []
    item = w.store._item(key)
    assert (item["exec_run_id"], item["exec_principal"]) == ("R3", "workerA")
    w.store.worker_alarm(w.p("workerA"), {"card": key, "reason": "live", **identity("R3")})
    with pytest.raises(Refused) as exc:
        w.store.worker_alarm(w.p("workerB"), {"card": key, "reason": "wrong", **identity("R3")})
    assert exc.value.code == "forbidden"
    assert w.store.verify_integrity() == []
