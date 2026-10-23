"""Production claim boundary and explicit hand-back validation."""
import pytest

from keeper_util import seed, to_claimed
from plangraph.keeper.errors import Refused
from test_keeper_exec_expected_state import w, doors, LIVE_SET
from test_keeper_exec_verbs import RUN1, complete


def test_unclaimed_delegate_cannot_record(w):
    key, = seed(w, [("A", [])])
    # Even an old snapshot with a stray delegate cannot authorize acquisition.
    w.conn.execute("UPDATE items SET delegate='workerA' WHERE key=?", (key,))
    w.conn.commit()
    with pytest.raises(Refused) as err:
        w.store.worker_complete(w.p("workerA"), complete(key, **RUN1))
    assert err.value.code == "forbidden"


@pytest.mark.parametrize("target,run", [("running", None), ("open", ("wf", "run", 1))])
def test_invalid_hand_back_is_validation(w, target, run):
    key, = seed(w, [("A", [])])
    with pytest.raises(Refused) as err:
        w.store.set_state(w.p("router"), key, target, hand_back=True, run=run)
    assert err.value.code == "validation"


def test_clear_audit_uses_verb_actor_and_time(w):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    w.store.worker_complete(w.p("workerA"), complete(key, **RUN1))
    due = w.clock() + 8000
    w.store.reap(w.p("system"), now=due)
    event = w.conn.execute(
        "SELECT actor,ts FROM events WHERE key=? AND type='exec_owner_changed'"
        " ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
    assert tuple(event) == ("system", w.store._iso(due))


def test_phase_claim_ask_park_complete(w, doors, tmp_path, monkeypatch):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "base"],
                   check=True, capture_output=True)
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(repo))
    key, = seed(w, [("A", [])])
    from test_planner_service import card
    w.store.put_card(w.p("router"), key, card())
    status, claim = doors.http("workerA", "POST", "/v1/worker_claim", {"card": key})
    assert status == 200 and claim["card"] == key
    assert w.store._item(key)["state"] == "claimed"
    status, question = doors.http("workerA", "POST", "/v1/ask_question", {
        "key": key, "text": "Which option?", "expected_state": LIVE_SET,
        **{"exec_" + k: v for k, v in RUN1.items()}})
    assert status == 200 and question["id"]
    assert doors.http("workerA", "POST", "/v1/park", {
        "card": key, "exit_class": "needs_input", "expected_state": LIVE_SET, **RUN1}) == (204, "")
    assert doors.http("workerA", "POST", "/v1/complete", complete(
        key, expected_state="parked", **RUN1)) == (204, "")
    assert w.store._item(key)["exec_principal"] == "workerA"
