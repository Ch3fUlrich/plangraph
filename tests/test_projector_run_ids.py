"""Projector-established execution ownership, through the real history/apply path."""
import pytest

from keeper_util import World, seed, to_claimed
from plangraph.keeper.errors import Refused
from plangraph.projector.apply import Projector
from plangraph.projector.history import project_history
from test_keeper_exec_projector_path import history
from test_keeper_worker_principal import require_backend


@pytest.fixture(params=["sqlite", "postgres"])
def world(tmp_path, request):
    require_backend(request.param)
    w = World(tmp_path, backend=request.param)
    seed(w, [("A", [])])
    yield w
    w.close()


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
        return getattr(w.store, "worker_" + verb)(w.p("workerA"), body)
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
