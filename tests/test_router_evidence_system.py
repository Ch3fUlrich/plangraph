"""Keep the system router-evidence exception scoped to phase merge proof."""

import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "keeper")
    world.store.create_items(
        world.p("router"),
        [defn("C1"), defn("GOAL", ["C1"], kind="goal")],
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
def test_authorized_evidence_is_not_role_refused(w, who, kind):
    try:
        w.store.add_evidence_router(
            w.p(who), "demo:C1", kind, {"commit": "a" * 40, "id": "D-1"}
        )
    except Refused as exc:
        assert exc.code != "forbidden"


@pytest.mark.parametrize("who", ["workerA", "l1main"])
@pytest.mark.parametrize("kind", ["commit", "decision", "commit+ci"])
def test_worker_and_l1_cannot_use_router_evidence_door(w, who, kind):
    with pytest.raises(Refused) as exc:
        w.store.add_evidence_router(
            w.p(who), "demo:C1", kind, {"commit": "a" * 40, "id": "D-1"}
        )
    assert exc.value.code == "forbidden"
