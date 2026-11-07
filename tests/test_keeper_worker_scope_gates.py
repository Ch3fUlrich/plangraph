"""Scoped planner gates and transport-to-store proofs, on both backends."""
import pytest

from test_keeper_worker_principal import scoped
from keeper_util import seed
from plangraph.keeper import api_routes, auth
from plangraph.keeper.api import KeeperHandler
from plangraph.keeper.errors import Refused
from plangraph.planner.phase import plan_phase
from plangraph.planner.phase_finish import finish_phase, attach_merge_evidence
from plangraph.planner.worker_claim import worker_claim


def invoke_gate(name, world, principal, key):
    if name == "phase_plan":
        return plan_phase(world.store, principal, key, repo_root=".", now=None)
    if name == "phase_finish":
        return finish_phase(world.store, principal, key, "invalid", "invalid",
                            repo_root=".", github=None)
    if name == "attach_merge_evidence":
        return attach_merge_evidence(world.store, principal, key, "invalid", github=None)
    return worker_claim(world.store, principal, key, repo_root=None)


@pytest.mark.parametrize("gate", ["phase_plan", "phase_finish", "attach_merge_evidence", "worker_claim"])
def test_each_planner_gate_accepts_scope_only(scoped, gate):
    world, principal = scoped
    with pytest.raises(Refused) as error:
        invoke_gate(gate, world, principal, "demo:missing")
    assert error.value.code != "forbidden"
    for role in ("reader", "phase-worker", "l1", "oversight", "spawner"):
        ordinary = auth.Principal("ordinary", role, ["demo"])
        with pytest.raises(Refused, match="requires"):
            invoke_gate(gate, world, ordinary, "demo:missing")
    with pytest.raises(Refused, match="not scoped to project"):
        invoke_gate(gate, world, principal, "other:C1")


TRANSPORT_ACTIONS = {
    "attempts": "worker_attempt", "alarms": "worker_alarm",
    "locks:acquire": "lock_acquire", "locks:release": "lock_release",
}


@pytest.mark.parametrize("transport,action", TRANSPORT_ACTIONS.items())
def test_named_transport_action_runs(scoped, transport, action):
    world, principal = scoped
    key, = seed(world, [("C1", [])])
    api_routes.run_gate(principal, transport.split(":")[0])
    if action == "worker_attempt":
        world.store.worker_attempt(principal, {"card": key, "n": 1, "exit_class": "ok"})
    elif action == "worker_alarm":
        world.store.worker_alarm(principal, {"card": key, "reason": "test"})
    else:
        fence = world.store.lock_acquire(principal, key, {"owner": "owner", "ttl_s": 30})
        if action == "lock_release":
            world.store.lock_release(principal, key, fence)
    assert world.conn.execute(
        "SELECT count(*) FROM audit_log WHERE actor=? AND action=? AND ok=1",
        (principal.name, action)).fetchone()[0] == 1


@pytest.mark.parametrize("verb", ["claim", "worker_claim", "show", "attempts", "park",
    "complete", "alarms", "lock_acquire", "lock_release", "ask_question", "answer_question",
    "phase_plan", "phase_finish"])
def test_every_worker_verb_refuses_other_project(scoped, verb):
    world, principal = scoped
    key, = seed(world, [("C1", [])], project="other")
    assert key == "other:C1"
    with pytest.raises(Refused, match="not scoped to project"):
        if verb in ("phase_plan", "phase_finish", "worker_claim"):
            getattr(api_routes, "_" + verb)(world.store, principal,
                {"key": key, "card": key, "branch": "invalid", "head": "invalid"},
                KeeperHandler._need)
        elif verb in ("claim", "show"):
            getattr(world.store, verb)(principal, key)
        elif verb == "ask_question":
            world.store.ask_question(principal, key, "question")
        elif verb == "answer_question":
            question = world.store.ask_question(world.p("router"), key, "question",
                default_answer="no", deadline=world.store._iso(world.clock() + 1))
            world.clock.advance(2)
            world.store.answer_question(principal, question["id"], source="question_timeout")
        elif verb.startswith("lock_"):
            getattr(world.store, verb)(principal, key, {"owner": "owner", "ttl_s": 30})
        else:
            method = {"attempts": "attempt", "alarms": "alarm"}.get(verb, verb)
            getattr(world.store, "worker_" + method)(principal, {"card": key})
