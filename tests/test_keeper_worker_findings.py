"""Final-round fail-closed and backwards-compatibility regressions."""
import pytest

from keeper_util import seed
from test_keeper_worker_principal import scoped
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused


@pytest.mark.parametrize("method", ["lock_acquire", "lock_release"])
@pytest.mark.parametrize("repo", ["repo", "owner/repo", "other:repo"])
def test_lock_requires_project(scoped, method, repo):
    world, principal = scoped
    with pytest.raises(Refused, match="project"):
        getattr(world.store, method)(principal, repo, {"owner": "owner", "ttl_s": 30})


@pytest.mark.parametrize("method", ["show", "worker_claim_lease", "snapshot"])
def test_plain_phase_worker_is_refused(scoped, method):
    world, principal = scoped
    key, = seed(world, [("A", [])])
    plain = auth.Principal(principal.name, principal.role, principal.projects)
    with pytest.raises(Refused, match="scope"):
        getattr(world.store, method)(plain, *([] if method == "snapshot" else [key]))


def test_legacy_answer_keeps_null_audit_key(scoped):
    world, _ = scoped
    key, = seed(world, [("A", [])])
    question = world.store.ask_question(world.p("router"), key, "question")
    world.store.answer_question(world.p("router"), question["id"], "answer")
    row = world.conn.execute(
        "SELECT key FROM audit_log WHERE action='answer_question'").fetchone()
    assert row["key"] is None


def test_revoked_answer_authenticates_before_question_read(scoped):
    world, principal = scoped
    if getattr(world.conn, "backend", "sqlite") != "sqlite":
        pytest.skip("SQLite statement tracing")
    auth.revoke(world.conn, principal.name)
    statements = []
    world.conn.set_trace_callback(statements.append)
    try:
        with pytest.raises(Refused):
            world.store.answer_question(principal, 1, source="question_timeout")
    finally:
        world.conn.set_trace_callback(None)
    assert not any("FROM questions" in sql for sql in statements)


def test_shipped_pg_principals_have_no_role_constraint():
    from plangraph.keeper.schema_pg_baseline import BASELINE
    definition = BASELINE.split("CREATE TABLE principals (", 1)[1].split(");", 1)[0]
    assert "CHECK" not in definition
