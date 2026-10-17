"""MODEL-LOOP: kind ``loop`` is a legal item kind, unknown kinds still refused.

Mutation killed by these tests: dropping ``loop`` from ``KINDS`` makes
``test_loop_kind_accepted`` fail; skipping ``check_kind`` in ``_normalise``
makes ``test_bogus_kind_still_refused`` fail.
"""
import pytest

from keeper_util import World, defn, seed
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


def _item(w, key):
    return w.store._item(key)


def test_loop_kind_accepted(w):
    w.store.create_items(w.p("router"), [
        defn("L", kind="loop"),
        defn("EXIT", ["L"], kind="decision"),
        defn("G2", ["EXIT"], kind="goal")])
    loop = _item(w, "demo:L")
    assert loop["kind"] == "loop"
    assert loop["operator"] == 0 and loop["wait"] is None
    assert verify_integrity(w.conn) == []


def test_bogus_kind_still_refused(w):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("X", kind="bogus")])
    assert exc.value.code == "validation" and "kind" in exc.value.msg
    assert _item(w, "demo:X") is None


def test_loop_without_exit_refused_at_create(w):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [
            defn("L", kind="loop"), defn("G2", ["L"], kind="goal")])
    assert exc.value.code == "validation" and "loop_no_exit" in exc.value.msg
    assert _item(w, "demo:L") is None
