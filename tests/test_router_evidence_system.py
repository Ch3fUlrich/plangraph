"""Keep the system router-evidence exception scoped to phase merge proof."""

import subprocess

import pytest

from keeper_util import FakeVerifier, World, defn
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_git import GitAdapter


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "keeper")
    world.store.verifiers["commit"] = FakeVerifier()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("commit", "--allow-empty", "-m", "base")
    world.non_merge = git("rev-parse", "HEAD")
    git("checkout", "-b", "phase")
    git("commit", "--allow-empty", "-m", "phase")
    git("checkout", "main")
    git("merge", "--no-ff", "phase", "-m", "phase merge")
    world.merge = git("rev-parse", "HEAD")
    world.store.ctx = {"git": GitAdapter({"demo": str(repo)})}
    world.store.create_items(
        world.p("router"),
        [defn("C1"), defn("GATE", ["C1"], kind="gate"),
         defn("GOAL", ["GATE"], kind="goal")],
    )
    world.store.conn.execute("UPDATE items SET state='in_review' WHERE key='demo:C1'")
    world.store.conn.commit()
    yield world
    world.close()


@pytest.mark.parametrize("kind", ["commit", "decision"])
def test_system_cannot_attach_non_merge_evidence(w, kind):
    with pytest.raises(Refused) as exc:
        w.store.add_evidence_router(
            w.p("system"), "demo:C1", kind, {"commit": "a" * 40, "id": "D-1"}
        )
    assert exc.value.code == "forbidden"


@pytest.mark.parametrize("who,kind", [
    ("system", "commit+ci"),
    ("router", "commit"),
    ("router", "decision"),
    ("router", "commit+ci"),
])
@pytest.mark.parametrize("case", ["allowed", "non_merge", "non_gate", "claimed", "cancelled_gate"])
def test_authorized_evidence_is_not_role_refused(w, who, kind, case):
    if case == "non_gate":
        w.conn.execute("DELETE FROM edges WHERE src='demo:GATE'")
    elif case == "claimed":
        w.conn.execute("UPDATE items SET state='claimed' WHERE key='demo:C1'")
    elif case == "cancelled_gate":
        w.conn.execute("UPDATE items SET state='cancelled' WHERE key='demo:GATE'")
    w.conn.commit()
    ref = {"commit": w.non_merge if case == "non_merge" else w.merge, "id": "D-1"}
    if who == "system" and case != "allowed":
        with pytest.raises(Refused):
            w.store.add_evidence_router(w.p(who), "demo:C1", kind, ref)
        assert w.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    else:
        w.store.add_evidence_router(w.p(who), "demo:C1", kind, ref)
        row = w.conn.execute("SELECT key,kind,ref FROM evidence").fetchone()
        assert w.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
        assert row["key"] == "demo:C1"
        assert row["kind"] == kind
        assert ref["commit"] in row["ref"]


@pytest.mark.parametrize("who", ["workerA", "l1main"])
@pytest.mark.parametrize("kind", ["commit", "decision", "commit+ci"])
def test_worker_and_l1_cannot_use_router_evidence_door(w, who, kind):
    with pytest.raises(Refused) as exc:
        w.store.add_evidence_router(
            w.p(who), "demo:C1", kind, {"commit": "a" * 40, "id": "D-1"}
        )
    assert exc.value.code == "forbidden"
