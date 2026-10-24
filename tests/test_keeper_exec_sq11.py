"""Retry idempotence and token-bound worker claim authorization."""
import pytest

from keeper_util import seed, seed_exec_owner, to_claimed
from plangraph.keeper.errors import Refused
from test_keeper_exec_expected_state import w, doors, LIVE_SET


def check_duplicate_complete(w, who):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    seed_exec_owner(w, key, ("wf", "run", 1))
    # Projected completion can retain the claim whose lease later expires.
    w.conn.execute("UPDATE items SET state='done' WHERE key=?", (key,))
    w.conn.commit()
    due = w.store._parse_iso(w.store._item(key)["lease_until"]).timestamp()
    w.clock.advance(due - w.clock())
    before = dict(w.store._item(key))
    body = dict(card=key, merged_sha="abcdef0", workflow_id="wf",
                run_id="run", attempt=1, expected_state="running")
    if who == "workerA":
        assert w.store.worker_complete(w.p(who), body)["card"] == key
    else:
        with pytest.raises(Refused) as exc:
            w.store.worker_complete(w.p(who), body)
        assert exc.value.code == "forbidden"
    assert dict(w.store._item(key)) == before


@pytest.mark.parametrize("who", ["workerA", "workerB"])
def test_duplicate_complete(w, who):
    check_duplicate_complete(w, who)


@pytest.mark.parametrize("who,expired", [("workerA", True), ("workerB", False),
                                         ("workerA", False)])
def test_question_claim_binding(w, who, expired):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    if expired:
        due = w.store._parse_iso(w.store._item(key)["lease_until"]).timestamp()
        w.clock.advance(due - w.clock())
    before = dict(w.store._item(key))
    if expired or who == "workerB":
        with pytest.raises(Refused) as exc:
            w.store.ask_question(w.p(who), key, "help")
        assert exc.value.code == ("epoch_stale" if expired else "forbidden")
        assert w.conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0
        assert dict(w.store._item(key)) == before
    else:
        assert w.store.ask_question(w.p(who), key, "help")["state"] == "open"


def test_complete_phase_holder_one_guard_one_clock(w, monkeypatch):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    calls = []
    original = w.store._park_guard
    def guard(principal, item, epoch, now):
        calls.append(now)
        return original(principal, item, epoch, now)
    monkeypatch.setattr(w.store, "_park_guard", guard)
    now = w.clock()
    w.store.worker_complete(w.p("workerA"), dict(
        card=key, merged_sha="abcdef0", expected_state=LIVE_SET))
    assert calls == [now]


@pytest.mark.parametrize("verb", ["complete", "alarms", "park", "ask_question"])
@pytest.mark.parametrize("role", ["router", "system"])
def test_http_worker_cannot_impersonate(w, doors, verb, role):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerB")
    bodies = {"complete": dict(card=key, merged_sha="abcdef0"),
              "alarms": dict(card=key, reason="help"),
              "park": dict(card=key, tier="free", exit_class="check_fail"),
              "ask_question": dict(key=key, text="help")}
    status, response = doors.http("workerA", "POST", "/v1/" + verb,
                                  dict(bodies[verb], role=role))
    assert (status, response["error"]) == (403, "forbidden")
