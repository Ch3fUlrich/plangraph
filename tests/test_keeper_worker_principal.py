"""The opt-in identity's transport and audited store allowlists."""
import os
import pytest

from keeper_util import World
from plangraph.keeper import auth
from plangraph.keeper.api_routes import READ_VERBS, WRITE_VERBS, run_gate
from plangraph.keeper.errors import Refused
from plangraph.keeper.worker_policy import ACTIONS, VERBS


def require_backend(backend):
    if backend == "postgres" and not os.environ.get("PG_DSN"):
        if os.environ.get("PG_KEEPER_BACKEND") == "postgres":
            pytest.fail("PostgreSQL declared but PG_DSN not configured")
        pytest.skip("PG_DSN not configured")


@pytest.fixture(params=["sqlite", "postgres"])
def scoped(tmp_path, request):
    require_backend(request.param)
    world = World(tmp_path, backend=request.param)
    token = auth.add_principal(world.conn, "phase-runner", "phase-worker", ["demo"])
    yield world, auth.authenticate(world.conn, token)
    world.close()


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_each_worker_transport_verb(scoped, verb):
    _, principal = scoped
    run_gate(principal, verb)


@pytest.mark.parametrize("verb", sorted((READ_VERBS | WRITE_VERBS) - VERBS))
def test_every_other_transport_verb_refused(scoped, verb):
    _, principal = scoped
    with pytest.raises(Refused, match="scope excludes"):
        run_gate(principal, verb)


def test_worker_store_lifecycle(scoped):
    from keeper_util import seed
    world, principal = scoped
    key, = seed(world, [("A", [])])
    from test_planner_phase import card
    world.store.put_card(world.p("router"), key, card())
    assert world.store.worker_claim_lease(principal, key)["epoch"] == 1
    assert world.store.get_card(principal, key)
    assert world.store.worker_attempt(principal, {"card": key, "n": 1,
        "exit_class": "ok"})["n"] == 1
    assert world.store.worker_alarm(principal, {"card": key, "reason": "test"})
    assert world.store.worker_complete(principal, {"card": key, "merged_sha": "a" * 40})
    question = world.store.ask_question(principal, key, "question", default_answer="no",
        deadline=world.store._iso(world.clock() + 1))
    world.clock.advance(2)
    with pytest.raises(Refused, match="may not do this"):
        world.store.answer_question(principal, question["id"], source="question_timeout")
    assert world.store.answer_question(world.p("router"), question["id"], source="question_timeout")
    assert world.store.worker_park(principal, {"card": key, "exit_class": "ok"})
    rows = world.conn.execute("SELECT action FROM audit_log WHERE actor=? AND ok=1",
                              (principal.name,)).fetchall()
    assert "claim" in {row["action"] for row in rows}


def test_store_refuses_outside_scope_and_old_tokens_unchanged(scoped):
    world, principal = scoped
    with pytest.raises(Refused, match="scope excludes"):
        world.store.snapshot(principal)
    for name in world.tokens:
        assert auth.authenticate(world.conn, world.tok(name)) == world.p(name)
    assert world.store.snapshot(world.p("system")) is not None


def test_scope_matches_rust_client():
    import re
    from pathlib import Path
    source = Path("rust/worker/src/keeper.rs").read_text()
    paths = set(re.findall(r'"/v1/([a-z_]+)', source))
    assert paths == VERBS


def test_scoped_worker_claim_show_and_locks(scoped):
    from keeper_util import seed
    world, principal = scoped
    key, = seed(world, [("A", [])])
    world.store.claim(principal, key)
    assert world.store.show(principal, key)
    fence = world.store.lock_acquire(principal, key, {"owner": "owner", "ttl_s": 30})
    assert fence
    assert world.store.lock_release(principal, key, fence) is not None


def test_scoped_phase_routes(scoped, tmp_path, monkeypatch):
    from plangraph.keeper import api_routes
    from plangraph.keeper.api import KeeperHandler
    from test_phase_finish import FakeGitHub, finish_cards, gate, git_repo
    from test_planner_phase import card

    world, principal = scoped
    gate(world)
    actions = []
    original_run = world.store._run

    def observed(who, action, key, callback):
        if who.name == principal.name:
            assert who.role == "phase-worker"
            actions.append(action)
        return original_run(who, action, key, callback)

    monkeypatch.setattr(world.store, "_run", observed)
    for key in ("demo:C1", "demo:C2"):
        world.store.put_card(world.p("router"), key, card())
    root, head = git_repo(tmp_path)
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    fake = FakeGitHub()
    monkeypatch.setattr(api_routes, "_github_from_env", lambda: fake)
    need = KeeperHandler._need
    run_gate(principal, "phase_plan")
    plan = api_routes._phase_plan(world.store, principal, {"key": "demo:PH-X"}, need)
    assert {c["key"] for c in plan["cards"]} == {"demo:C1", "demo:C2"}
    finish_cards(world, "demo:C1", "demo:C2")
    run_gate(principal, "phase_finish")
    result = api_routes._phase_finish(world.store, principal, {
        "key": "demo:PH-X", "branch": "phase/demo-PH-X", "head": head}, need)
    assert result["state"] == "merged"
    assert result["merge_sha"] == fake.merge_sha
    assert "attach_merge_evidence" in actions
    assert world.conn.execute(
        "SELECT count(*) FROM evidence WHERE kind='commit+ci'").fetchone()[0] == 2


@pytest.mark.parametrize("route", ["phase_plan", "phase_finish"])
def test_missing_phase_cross_project_refused(scoped, route):
    from plangraph.keeper import api_routes
    from plangraph.keeper.api import KeeperHandler
    world, principal = scoped
    with pytest.raises(Refused, match="not scoped to project"):
        getattr(api_routes, "_" + route)(world.store, principal, {
            "key": "other:PH-X", "branch": "phase/other-PH-X", "head": "a" * 40,
        }, KeeperHandler._need)


@pytest.mark.parametrize("route", ["phase_plan", "phase_finish"])
def test_phase_helpers_enforce_scope_without_run_gate(scoped, monkeypatch, route):
    from plangraph.keeper import api_routes, worker_policy
    from plangraph.keeper.api import KeeperHandler
    world, principal = scoped
    monkeypatch.setattr(worker_policy, "VERBS", worker_policy.VERBS - {route})
    with pytest.raises(Refused, match="scope excludes"):
        getattr(api_routes, "_" + route)(world.store, principal, {
            "key": "demo:PH-X", "branch": "phase/demo-PH-X", "head": "a" * 40,
        }, KeeperHandler._need)


@pytest.mark.parametrize("edit", ["role='system'", "projects='[\"other\"]'", "active=0"])
def test_principal_changes_revoke_authenticated_worker(scoped, edit):
    from keeper_util import seed
    world, principal = scoped
    key, = seed(world, [("A", [])])
    if getattr(world.conn, "backend", "sqlite") == "postgres":
        world.conn._raw.execute("DROP TRIGGER trg_principals_revoke_only_update ON principals")
    else:
        # Simulate an operator edit, outside the keeper's DDL guard.
        world.conn.set_authorizer(None)
        world.conn.execute("DROP TRIGGER trg_principals_revoke_only_update")
    world.conn.execute("UPDATE principals SET " + edit + " WHERE name=?", (principal.name,))
    with pytest.raises(Refused, match="changed since authentication|revoked or inactive"):
        world.store.show(principal, key)


def test_worker_is_not_system(scoped):
    world, principal = scoped
    assert principal.role == "phase-worker"
    with pytest.raises(Refused, match="may not do this"):
        world.store._require_role(principal, "system")


def test_other_project_store_and_answer_refused(scoped):
    from keeper_util import seed
    world, principal = scoped
    key, = seed(world, [("B", [])], project="other")
    with pytest.raises(Refused, match="not scoped to project"):
        world.store.show(principal, key)
    question = world.store.ask_question(world.p("router"), key, "question",
        default_answer="no", deadline=world.store._iso(world.clock() + 1))
    world.clock.advance(2)
    with pytest.raises(Refused, match="not scoped to project"):
        world.store.answer_question(principal, question["id"], source="question_timeout")


def test_existing_roles_do_not_gain_worker_rights(scoped):
    world, _ = scoped
    with pytest.raises(Refused, match="may not do this"):
        world.store.worker_claim_lease(world.p("reader"), "demo:A")
    with pytest.raises(Refused, match="may not do this"):
        world.store._require_role(world.p("workerA"), "phase-worker")
