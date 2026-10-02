"""B2: L5 Closes: fail-closed, the ``priority`` batch op and the seat/earlier lows."""
import pytest

from keeper_util import World, defn, seed
from plangraph.keeper.errors import Refused
from test_keeper_apply_ops import create, link_goal, snap
from test_keeper_close import DID, KEY, _world


def _row(cell):
    return f"| {DID} | note | {cell} |\n"


# ------------------------------------------------------ L5: Closes: fail-closed
@pytest.mark.parametrize("cell", ["Closes: demo: A", "Closes:", "Closes: ,",
                                  "Closes: ,  ,", "Closes: demo:"])
def test_closes_keyword_without_valid_token_fails_closed(tmp_path, cell):
    w = _world(tmp_path, _row(cell))
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert w.store._item(KEY)["state"] != "done"
    w.close()


def test_valid_closes_token_closes(tmp_path):
    w = _world(tmp_path, _row("Closes: demo:A"))
    assert w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)["state"] == "done"
    w.close()


def test_no_closes_keyword_batch_ref_authorises(tmp_path):
    w = _world(tmp_path, f"| {DID} | recorded, no token |\n")
    assert w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)["state"] == "done"
    w.close()


def test_fail_closed_also_inside_apply_ops(tmp_path):
    w = _world(tmp_path, _row("Closes:"))
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [
            create("X"), link_goal("A", "X"),
            {"op": "close_on_decision", "key": KEY}], batch_ref=w.batch_ref)
    assert exc.value.code == "evidence_unverified" and snap(w) == before
    w.close()
