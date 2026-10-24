"""Claim leases apply to production worker bodies without expected_state."""
import pytest

from keeper_util import seed, seed_exec_owner, to_claimed
from plangraph.keeper.errors import Refused
from test_keeper_exec_expected_state import w


@pytest.mark.parametrize("verb", ["complete", "alarm"])
@pytest.mark.parametrize("owned", [False, True])
def test_expired_production_claim(w, verb, owned):
    check_expired_claim(w, verb, owned)


def check_expired_claim(w, verb, owned):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    if owned:
        seed_exec_owner(w, key, ("wf", "run", 1))
    due = w.store._parse_iso(w.store._item(key)["lease_until"]).timestamp()
    w.clock.advance(due - w.clock())
    before = dict(w.store._item(key))
    body = packet(key, verb)
    if owned:
        body.update(workflow_id="wf", run_id="run", attempt=2)
    with pytest.raises(Refused) as exc:
        getattr(w.store, "worker_" + verb)(w.p("workerA"), body)
    assert exc.value.code == "epoch_stale"
    assert "claim lease expired" in str(exc.value)
    assert dict(w.store._item(key)) == before
    assert w.conn.execute(
        "SELECT COUNT(*) FROM projector_alarms WHERE key=?", (key,)
    ).fetchone()[0] == 0


def packet(key, verb):
    return {"card": key, **({"merged_sha": "abcdef0"} if verb == "complete"
                           else {"reason": "stuck"})}


@pytest.mark.parametrize("verb", ["complete", "alarm"])
@pytest.mark.parametrize("who", ["workerA", "workerB", "router", "system"])
def test_production_claim_authorization(w, verb, who):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    if who in ("router", "system"):
        due = w.store._parse_iso(w.store._item(key)["lease_until"]).timestamp()
        w.clock.advance(due - w.clock())
    before = dict(w.store._item(key))
    if who == "workerB":
        with pytest.raises(Refused) as exc:
            getattr(w.store, "worker_" + verb)(w.p(who), packet(key, verb))
        assert exc.value.code == "forbidden"
        assert dict(w.store._item(key)) == before
    else:
        assert getattr(w.store, "worker_" + verb)(w.p(who), packet(key, verb))["card"] == key
