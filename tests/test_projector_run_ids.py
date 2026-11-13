"""Projector-established execution ownership, through the real history/apply path."""
import pytest

from keeper_util import seed, to_claimed
from plangraph.keeper.errors import Refused
from plangraph.projector.apply import Projector
from plangraph.projector.history import project_history
from test_keeper_exec_projector_path import history
from test_keeper_worker_principal import scoped


@pytest.fixture
def world(scoped):
    w, _ = scoped
    seed(w, [("A", [])])
    return w


def project(w, run="R2", *, ordinal=1, attempt=1):
    import base64
    import json
    h = history("needs_input")
    h["events"][1]["activityTaskScheduledEventAttributes"]["input"] = {"payloads": [{
        "metadata": {"encoding": "anNvbi9wbGFpbg=="},
        "data": base64.b64encode(json.dumps({"n": attempt, **identity(run, attempt)}).encode()).decode()}]}
    projection = project_history(h, workflow_id="card:demo:A", run_id=run)
    return Projector(w.store, w.p("system"), now=w.clock).apply_run(projection, run_ord=ordinal)


def identity(run="R2", attempt=1):
    return {"workflow_id": "card:demo:A", "run_id": run, "attempt": attempt}


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
            body.update(exit_class="needs_input", expected_state="needs_input" if not claimed else None)
        elif verb == "alarm":
            body.update(reason="question")
        else:
            body.update(n=1, exit_class="ok")
        method = getattr(w.store, "worker_" + verb, None)
        assert callable(method), f"missing worker verb: {verb}"
        return method(w.p("workerA"), body)
    with pytest.raises(Refused) as exc:
        call("R1")
    assert exc.value.code == "exec_stale"
    assert dict(w.store._item(key)) == before
    call("R2")  # No release/reaper between the refused stale verb and the live verb.
    item = w.store._item(key)
    assert (item["exec_workflow_id"], item["exec_run_id"], item["exec_attempt"]) == (
        "card:demo:A", "R2", 1)
    assert w.store.verify_integrity() == []


def test_projection_records_workflow_attempt_not_activity_retry(world):
    project(world, attempt=3)
    item = world.store._item("demo:A")
    assert (item["exec_run_id"], item["exec_attempt"]) == ("R2", 3)
    with pytest.raises(Refused) as exc:
        world.store.worker_alarm(world.p("workerA"), {
            "card": "demo:A", "reason": "late", **identity(attempt=2)})
    assert exc.value.code == "exec_stale"


def test_newer_projected_run_retires_old_owner_and_old_projection_is_stale(world):
    project(world, "R1", ordinal=0)
    project(world)
    assert world.store._item("demo:A")["exec_run_id"] == "R2"
    assert project(world, "R0", ordinal=0).states_stale == 2
    assert world.store._item("demo:A")["exec_run_id"] == "R2"


def test_second_scheduled_attempt_advances_owner_even_while_running(world):
    import base64
    import json
    from test_keeper_exec_projector_path import event
    h = history("ok")
    h["events"][-1] = event(5, "ACTIVITY_TASK_SCHEDULED", activityId="attempt-2",
        activityType={"name": "attempt"}, input={"payloads": [{
            "metadata": {"encoding": "anNvbi9wbGFpbg=="},
            "data": base64.b64encode(json.dumps({"n": 2}).encode()).decode()}]})
    p = project_history(h, workflow_id="card:demo:A", run_id="R2")
    Projector(world.store, world.p("system")).apply_run(p, run_ord=1)
    assert world.store._item("demo:A")["exec_attempt"] == 2


@pytest.mark.parametrize("claimed", [False, True])
def test_claimed_projection_records_only_owner(world, claimed):
    from dataclasses import replace
    if claimed:
        to_claimed(world, "demo:A", "workerA")
    before = dict(world.store._item("demo:A"))
    p = project_history(history("ok"), workflow_id="card:demo:A", run_id="R2")
    p = replace(p, states=(replace(p.states[0], state="claimed"),))
    result = Projector(world.store, world.p("system")).apply_run(p, run_ord=1)
    assert (result.states_written, result.states_skipped_state) == (0, 1)
    item = world.store._item("demo:A")
    assert item["exec_run_id"] == "R2"
    assert all(item[k] == before[k] for k in (
        "state", "claimed_by", "delegate", "epoch", "lease_until"))
    assert world.store.verify_integrity() == []


@pytest.mark.parametrize("done", [False, True])
def test_claimed_target_does_not_bypass_keeper_owned_states(world, done):
    from keeper_util import to_in_review, pass_review
    to_in_review(world, "demo:A")
    if done:
        pass_review(world, "demo:A")
    before = dict(world.store._item("demo:A"))
    with pytest.raises(Refused) as exc:
        world.store.project_state(world.p("system"), "demo:A", "claimed", "history",
                                  run=("card:demo:A", "R2", 1))
    assert exc.value.code == ("locked" if done else "bad_state")
    assert dict(world.store._item("demo:A")) == before


@pytest.mark.parametrize("old_watermark", [False, True])
def test_worker_recorded_r2_refuses_unordered_r1_projection(world, old_watermark):
    if old_watermark:
        project(world, "R0", ordinal=0)
        world.store.set_state(world.p("router"), "demo:A", "open", hand_back=True)
    to_claimed(world, "demo:A", "workerA")
    body = {"card": "demo:A", "reason": "live", **identity()}
    world.store.worker_alarm(world.p("workerA"), body)
    before = dict(world.store._item("demo:A"))
    with pytest.raises(Refused) as exc:
        world.store.project_state(world.p("system"), "demo:A", "running", "late",
                                  run=("card:demo:A", "R1", 1))
    assert exc.value.code == "exec_stale"
    assert project(world, "R1", ordinal=1).states_skipped_state == 2
    assert dict(world.store._item("demo:A")) == before
    world.store.worker_alarm(world.p("workerA"), body)
    assert world.store._item("demo:A")["exec_run_id"] == "R2"
    project(world, "R2", ordinal=2)  # Establish R2's order without displacing it.
    project(world, "R3", ordinal=3)
    assert world.store._item("demo:A")["exec_run_id"] == "R3"
    assert world.store.verify_integrity() == []


def test_projected_supersession_keeps_claim_and_principal_consistent(world):
    to_claimed(world, "demo:A")
    world.store.delegate(world.p("l1main"), "demo:A", "workerA")
    project(world, "R1", ordinal=0)
    before = dict(world.store._item("demo:A"))
    project(world)
    item = world.store._item("demo:A")
    assert (item["exec_run_id"], item["exec_principal"]) == ("R2", "workerA")
    assert all(item[k] == before[k] for k in ("claimed_by", "delegate", "lease_until"))
    world.store.worker_alarm(world.p("workerA"), {
        "card": "demo:A", "reason": "live", **identity()})
    assert world.store.verify_integrity() == []


def test_refused_r3_retries_after_worker_recorded_r2_is_projected(world):
    to_claimed(world, "demo:A", "workerA")
    world.store.worker_park(world.p("workerA"), {
        "card": "demo:A", "exit_class": "needs_input", **identity()})
    before = dict(world.store._item("demo:A"))
    assert before["claimed_by"] is None and before["delegate"] is None
    assert project(world, "R3", ordinal=2).states_skipped_state == 2
    assert dict(world.store._item("demo:A")) == before
    assert world.conn.execute("SELECT 1 FROM projector_watermark").fetchone() is None
    assert world.conn.execute(
        "SELECT 1 FROM projector_applied WHERE run_id='R3' AND kind='state'").fetchone() is None
    project(world, "R2", ordinal=1)
    assert project(world, "R3", ordinal=2).states_written == 2
    item = world.store._item("demo:A")
    assert (item["exec_run_id"], item["exec_principal"]) == ("R3", "workerA")
    world.store.worker_alarm(world.p("workerA"), {
        "card": "demo:A", "reason": "live", **identity("R3")})
    with pytest.raises(Refused) as exc:
        world.store.worker_alarm(world.p("workerB"), {
            "card": "demo:A", "reason": "wrong worker", **identity("R3")})
    assert exc.value.code == "forbidden"
    assert world.store.verify_integrity() == []


@pytest.mark.parametrize("target", ["done", "cancelled", "in_review", "invalid"])
def test_nonsettable_projection_is_skipped_without_validation_outage(world, target):
    from dataclasses import replace
    p = project_history(history("ok"), workflow_id="card:demo:A", run_id="R2")
    p = replace(p, states=(replace(p.states[0], state=target),))
    before = dict(world.store._item("demo:A"))
    result = Projector(world.store, world.p("system")).apply_run(p, run_ord=1)
    assert (result.states_skipped_state, result.states_written) == (1, 0)
    assert dict(world.store._item("demo:A")) == before


def test_projection_has_distinct_audit_verb(world):
    project(world)
    rows = world.conn.execute(
        "SELECT action FROM audit_log WHERE actor='system' AND key='demo:A'").fetchall()
    verbs = [row["action"] for row in rows]
    assert verbs.count("project_state") == 2 and "set_state" not in verbs
