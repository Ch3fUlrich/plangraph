"""Contract tests for the keeper's worker verbs.

The contract is the Rust worker's HTTP client (rust/worker/src/keeper.rs):
a Bearer worker token; ``POST /v1/{attempts,park,complete,alarms}`` answer 204
with an empty body; ``POST /v1/locks/<repo>/acquire`` answers 200 ``{token,
owner}`` and ``.../release`` 204; ``<repo>`` is percent-encoded as ONE path
segment.  All SQL here is portable so the file runs on SQLite and Postgres.
"""
import json
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

from door_util import Doors
from keeper_util import World, seed, to_claimed, to_done
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


@pytest.fixture
def doors(w, tmp_path):
    live = Doors(w, tmp_path)
    try:
        yield live
    finally:
        live.close()


def lock_path(repo, action):
    return f"/v1/locks/{quote(repo, safe='')}/{action}"


def attempt(card, n=1):
    return {"card": card, "n": n, "leg": "free", "exit_class": "ok",
            "diff_hash": "deadbeef", "check_tail": "boom"}


def park(card, **extra):
    body = {"card": card, "tier": "free", "exit_class": "check_fail",
            "check_tail": "boom"}
    body.update(extra)
    return body


def state(w, key):
    return w.conn.execute("SELECT state FROM items WHERE key=?", (key,)).fetchone()[0]


# ------------------------------------------------------------------ attempts
def test_worker_attempt_is_204_audited_and_never_stored(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/attempts", attempt(key)) == (204, "")
    rows = w.conn.execute(
        "SELECT ok, action FROM audit_log WHERE action='worker_attempt'").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, "worker_attempt")]
    assert doors.http("workerA", "POST", "/v1/attempts", attempt(key)) == (204, "")
    assert w.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_worker_attempt_refusals(doors, w):
    key, = seed(w, [("A", [])])
    bad = dict(attempt(key), exit_class="nope")
    status, body = doors.http("workerA", "POST", "/v1/attempts", bad)
    assert (status, body["error"]) == (400, "validation")
    status, body = doors.http("workerA", "POST", "/v1/attempts", attempt("demo:ZZZ"))
    assert (status, body["error"]) == (404, "not_found")
    status, body = doors.http("l1other", "POST", "/v1/attempts", attempt(key))
    assert (status, body["error"]) == (403, "forbidden")
    status, body = doors.http("reader", "POST", "/v1/attempts", attempt(key))
    assert (status, body["error"]) == (403, "forbidden")


def test_attempt_without_a_bearer_token_is_401(doors, w):
    key, = seed(w, [("A", [])])
    req = urllib.request.Request(doors.url + "/v1/attempts",
                                 data=json.dumps(attempt(key)).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req)
        status = 200
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 401


# ---------------------------------------------------------------------- park
def test_park_moves_running_to_parked_and_is_idempotent(doors, w):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "running")
    body = {"card": key, "tier": "free", "exit_class": "check_fail",
            "check_tail": "boom"}
    assert doors.http("workerA", "POST", "/v1/park", body) == (204, "")
    assert state(w, key) == "parked"
    assert doors.http("workerA", "POST", "/v1/park", body) == (204, "")
    assert state(w, key) == "parked"


def test_park_in_another_project_is_403(doors, w):
    other, = seed(w, [("A", [])], project="other")
    status, body = doors.http("workerA", "POST", "/v1/park",
                              {"card": other, "tier": "free",
                               "exit_class": "check_fail", "check_tail": ""})
    assert (status, body["error"]) == (403, "forbidden")


# ------------------------------------------------------------------ complete
def test_complete_is_204_and_leaves_the_state_alone(doors, w):
    key, = seed(w, [("A", [])])
    was = state(w, key)
    assert doors.http("workerA", "POST", "/v1/complete",
                      {"card": key, "merged_sha": "deadbeef"}) == (204, "")
    assert state(w, key) == was


def test_complete_validates_the_sha_and_the_project(doors, w):
    key, = seed(w, [("A", [])])
    other, = seed(w, [("B", [])], project="other")
    status, body = doors.http("workerA", "POST", "/v1/complete",
                              {"card": key, "merged_sha": "zz"})
    assert (status, body["error"]) == (400, "validation")
    status, body = doors.http("workerA", "POST", "/v1/complete",
                              {"card": other, "merged_sha": "deadbeef"})
    assert (status, body["error"]) == (403, "forbidden")


# -------------------------------------------------------------------- alarms
def test_worker_alarm_stores_one_open_row_and_lists_it(doors, w):
    key, = seed(w, [("A", [])])
    body = {"card": key, "reason": "stuck on check"}
    assert doors.http("workerA", "POST", "/v1/alarms", body) == (204, "")
    rows = w.conn.execute(
        "SELECT kind, key, detail FROM projector_alarms").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("worker_alarm", key, "stuck on check")]
    assert doors.http("workerA", "POST", "/v1/alarms", body) == (204, "")
    assert len(w.conn.execute(
        "SELECT id FROM projector_alarms WHERE cleared_at IS NULL"
        ).fetchall()) == 1                                   # deduped
    status, listed = doors.http("router", "GET", "/v1/alarms")
    assert status == 200
    assert [(row["kind"], row["key"]) for row in listed["alarms"]] == [
        ("worker_alarm", key)]


def test_a_long_alarm_reason_is_clipped_not_refused(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/alarms",
                      {"card": key, "reason": "x" * 600}) == (204, "")
    assert w.conn.execute("SELECT detail FROM projector_alarms").fetchone()[0] \
        == "x" * 500


# --------------------------------------------------------------------- locks
def test_lock_acquire_release_chain_with_generation_tokens(doors, w):
    repo = "org/repo name"
    held = {"owner": "w1", "ttl_s": 60}
    assert doors.http("workerA", "POST", lock_path(repo, "acquire"), held) \
        == (200, {"token": 1, "owner": "workerA:w1"})
    status, body = doors.http("workerA", "POST", lock_path(repo, "acquire"),
                              {"owner": "w2", "ttl_s": 60})
    assert (status, body["error"]) == (409, "conflict")
    assert doors.http("workerA", "POST", lock_path(repo, "acquire"), held) \
        == (200, {"token": 1, "owner": "workerA:w1"})    # same owner: same token
    assert doors.http("workerA", "POST", lock_path(repo, "release"),
                      {"token": 1, "owner": "workerA:w1"}) == (204, "")
    status, body = doors.http("workerA", "POST", lock_path(repo, "release"),
                              {"token": 1, "owner": "workerA:w1"})   # no longer held
    assert (status, body["error"]) == (409, "conflict")
    assert doors.http("workerA", "POST", lock_path(repo, "acquire"),
                      {"owner": "w2", "ttl_s": 60}) \
        == (200, {"token": 2, "owner": "workerA:w2"})    # tokens only grow
    status, body = doors.http("workerA", "POST", lock_path(repo, "release"),
                              {"token": 1, "owner": "workerA:w1"})   # stale token
    assert (status, body["error"]) == (409, "conflict")


def test_an_expired_lock_is_regranted_and_the_old_release_fails(doors, w):
    assert doors.http("workerA", "POST", lock_path("r", "acquire"),
                      {"owner": "w1", "ttl_s": 10}) \
        == (200, {"token": 1, "owner": "workerA:w1"})
    w.clock.advance(11)
    assert doors.http("workerA", "POST", lock_path("r", "acquire"),
                      {"owner": "w2", "ttl_s": 10}) \
        == (200, {"token": 2, "owner": "workerA:w2"})
    status, body = doors.http("workerA", "POST", lock_path("r", "release"),
                              {"token": 1, "owner": "workerA:w1"})
    assert (status, body["error"]) == (409, "conflict")


def test_lock_validation_roles_and_unknown_actions(doors, w):
    status, body = doors.http("workerA", "POST", lock_path("r", "acquire"),
                              {"owner": "w1", "ttl_s": 0})
    assert (status, body["error"]) == (400, "validation")
    status, body = doors.http("reader", "POST", lock_path("r", "acquire"),
                              {"owner": "w1", "ttl_s": 5})
    assert (status, body["error"]) == (403, "forbidden")
    status, body = doors.http("workerA", "POST", lock_path("x", "steal"),
                              {"owner": "w1", "ttl_s": 5})
    assert (status, body["error"]) == (404, "not_found")


def test_the_lock_repo_is_stored_decoded(doors, w):
    assert doors.http("workerA", "POST", lock_path("org/repo name", "acquire"),
                      {"owner": "w1", "ttl_s": 5})[0] == 200
    rows = [r[0] for r in w.conn.execute("SELECT repo FROM merge_locks").fetchall()]
    assert rows == ["org/repo name"]


# ---------------------------------------------------------------------- show
def test_show_surfaces_attempts_and_cost(doors, w):
    key, = seed(w, [("A", [])])
    w.store.record_attempt(w.p("router"), key,
                           {"n": 1, "exit_class": "needs_input", "cost": 0.25})
    w.store.record_attempt(w.p("router"), key,
                           {"n": 2, "exit_class": "ok", "cost": 0.5})
    status, body = doors.http("router", "GET", f"/v1/show?key={key}&fmt=json")
    assert status == 200
    assert [(a["n"], a["exit_class"]) for a in body["attempts"]] == [
        (1, "needs_input"), (2, "ok")]
    assert body["attempt_cost"] == 0.75
    status, text = doors.http("router", "GET", f"/v1/show?key={key}&fmt=text")
    assert status == 200
    assert "attempts 1:needs_input; 2:ok | cost 0.75" in text


def test_show_of_an_item_without_attempts_is_empty_and_none(doors, w):
    keys = seed(w, [("A", []), ("B", [])])
    status, body = doors.http("router", "GET", f"/v1/show?key={keys[1]}&fmt=json")
    assert status == 200
    assert body["attempts"] == [] and body["attempt_cost"] is None


# ------------------------------------------------------------- store unit
def test_store_lock_acquire_as_reader_raises_forbidden(w):
    with pytest.raises(Refused) as exc:
        w.store.lock_acquire(w.p("reader"), "r", {"owner": "o", "ttl_s": 5})
    assert exc.value.code == "forbidden"


# -------------------------------------- park guards a claimed card
def test_park_by_a_stale_worker_after_reclaim_is_refused(doors, w):
    key, = seed(w, [("A", [])])
    epoch = to_claimed(w, key, "workerA", lease_s=60)
    w.store.release(w.p("workerA"), key, epoch)
    to_claimed(w, key, "workerB", lease_s=60)
    status, body = doors.http("workerA", "POST", "/v1/park", park(key))
    assert (status, body["error"]) == (403, "forbidden")
    assert state(w, key) == "claimed"


# ------------------------------------------- park checks the epoch
def test_park_with_a_stale_epoch_is_refused_and_current_epoch_is_ok(doors, w):
    key, = seed(w, [("A", [])])
    e1 = to_claimed(w, key, "workerA")
    w.store.release(w.p("workerA"), key, e1)
    e2 = to_claimed(w, key, "workerA")
    status, body = doors.http("workerA", "POST", "/v1/park", park(key, epoch=e1))
    assert (status, body["error"]) == (409, "epoch_stale")
    assert doors.http("workerA", "POST", "/v1/park", park(key, epoch=e2)) == (204, "")
    assert state(w, key) == "parked"


# ------------------------------- the holder parks without an epoch
def test_the_current_holder_parks_a_claimed_card_without_an_epoch(doors, w):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    assert doors.http("workerA", "POST", "/v1/park", park(key)) == (204, "")
    assert state(w, key) == "parked"
    row = w.conn.execute(
        "SELECT claimed_by, lease_until FROM items WHERE key=?", (key,)).fetchone()
    assert row[0] is None and row[1] is None
    assert doors.http("workerA", "POST", "/v1/park", park(key)) == (204, "")


# ------------------------------------------ park refuses an expired lease
def test_park_with_an_expired_lease_is_refused(doors, w):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA", lease_s=60)
    w.clock.advance(61)
    status, body = doors.http("workerA", "POST", "/v1/park", park(key))
    assert (status, body["error"]) == (409, "epoch_stale")
    assert state(w, key) == "claimed"


# ------------------------- a delegate and the router may park a claimed card
def test_a_delegate_and_the_router_may_park_a_claimed_card(doors, w):
    delegated, routed = seed(w, [("A", []), ("B", [])])
    to_claimed(w, delegated, "l1main")
    w.store.delegate(w.p("l1main"), delegated, "workerB")
    assert doors.http("workerB", "POST", "/v1/park", park(delegated)) == (204, "")
    to_claimed(w, routed, "l1main")
    assert doors.http("router", "POST", "/v1/park", park(routed)) == (204, "")
    assert state(w, delegated) == "parked" and state(w, routed) == "parked"
    for key in (delegated, routed):
        row = w.conn.execute(
            "SELECT claimed_by, delegate, lease_until FROM items WHERE key=?",
            (key,)).fetchone()
        assert row[0] is None and row[1] is None and row[2] is None


# -------------------------- a locked card gets the usual locked refusal
def test_park_of_a_locked_card_gets_the_usual_locked_refusal(doors, w):
    key, = seed(w, [("A", [])])
    to_done(w, key)
    status, body = doors.http("workerA", "POST", "/v1/park", park(key))
    assert (status, body["error"]) == (409, "locked")


# ------------------------------------ the lock owner binds to the principal
def test_lock_owner_is_bound_to_the_principal(doors, w):
    held = {"owner": "run1", "ttl_s": 60}
    assert doors.http("workerA", "POST", lock_path("r", "acquire"), held) \
        == (200, {"token": 1, "owner": "workerA:run1"})
    status, body = doors.http("workerB", "POST", lock_path("r", "acquire"), held)
    assert (status, body["error"]) == (409, "conflict")
    status, body = doors.http("workerB", "POST", lock_path("r", "acquire"),
                              {"owner": "workerA:run1", "ttl_s": 60})
    assert (status, body["error"]) == (409, "conflict")
    assert doors.http("workerA", "POST", lock_path("r", "acquire"), held) \
        == (200, {"token": 1, "owner": "workerA:run1"})  # same owner: same token
    status, body = doors.http("workerB", "POST", lock_path("r", "release"),
                              {"token": 1, "owner": "workerA:run1"})
    assert (status, body["error"]) == (409, "conflict")
    assert doors.http("workerB", "POST", lock_path("r", "release"),
                      {"token": 1, "owner": "run1"})[0] == 409
    assert doors.http("workerA", "POST", lock_path("r", "release"),
                      {"token": 1, "owner": "run1"})[0] == 409
    assert doors.http("workerA", "POST", lock_path("r", "release"),
                      {"token": 1, "owner": "workerA:run1"}) == (204, "")
    assert doors.http("workerB", "POST", lock_path("r", "acquire"), held) \
        == (200, {"token": 2, "owner": "workerB:run1"})


# ------------------------- the binding survives a colon in the principal name
def test_lock_owner_binding_survives_a_colon_in_the_principal_name(w):
    pa = auth.authenticate(w.conn, auth.add_principal(w.conn, "a:b", "worker",
                                                     ["demo"], family="famX"))
    pb = auth.authenticate(w.conn, auth.add_principal(w.conn, "a", "worker",
                                                     ["demo"], family="famY"))
    pc = auth.authenticate(w.conn, auth.add_principal(w.conn, "ab", "worker",
                                                     ["demo"], family="famZ"))
    assert w.store.lock_acquire(pa, "r", {"owner": "x", "ttl_s": 60}) \
        == {"token": 1, "owner": "a%3Ab:x"}
    with pytest.raises(Refused) as exc:
        w.store.lock_acquire(pb, "r", {"owner": "b:x", "ttl_s": 60})
    assert exc.value.code == "conflict"
    with pytest.raises(Refused) as exc:
        w.store.lock_release(pb, "r", {"token": 1, "owner": "a%3Ab:x"})
    assert exc.value.code == "conflict"
    with pytest.raises(Refused) as exc:
        w.store.lock_release(pb, "r", {"token": 1, "owner": "a:b:x"})
    assert exc.value.code == "conflict"
    assert w.store.lock_release(pa, "r",
                                {"token": 1, "owner": "a%3Ab:x"}) == {}
    assert w.store.lock_acquire(pb, "r2", {"owner": "y", "ttl_s": 60}) \
        == {"token": 1, "owner": "a:y"}
    with pytest.raises(Refused) as exc:
        w.store.lock_release(pc, "r2", {"token": 1, "owner": "a:y"})
    assert exc.value.code == "conflict"


# ------------------- park of a claimed card revokes its run tokens
class _Runs:
    """Minimal run record: run ids wa/wb name demo:A / demo:B."""

    _JOBS = {"wa": "demo:A", "wb": "demo:B"}

    def valid_run_id(self, run_id):
        return isinstance(run_id, str) and run_id.replace("-", "").isalnum()

    def job(self, run_id):
        if run_id not in self._JOBS:
            raise FileNotFoundError(run_id)
        return {"request": {"title": f"work on {self._JOBS[run_id]}"}, "task": ""}


def _revoked_at(w, key, token):
    return w.conn.execute(
        "SELECT revoked_at FROM run_tokens WHERE key=? AND token_sha256=?",
        (key, auth.hash_token(token))).fetchone()["revoked_at"]


def test_park_of_a_claimed_card_revokes_run_tokens_like_release(doors, w):
    w.store.ctx = {"runs": _Runs()}
    a, b, c = seed(w, [("A", []), ("B", []), ("C", [])])
    to_claimed(w, a, "l1main")
    to_claimed(w, b, "l1main")
    token_a = w.store.mint_run_token(w.p("l1main"), a, "writer", "wa")["token"]
    token_b = w.store.mint_run_token(w.p("l1main"), b, "writer", "wb")["token"]

    assert doors.http("router", "POST", "/v1/park", park(a)) == (204, "")

    assert _revoked_at(w, a, token_a) is not None
    assert _revoked_at(w, b, token_b) is None
    assert [p for p in w.store.verify_integrity()
            if p["code"].startswith("token_")] == []

    w.store.set_state(w.p("router"), c, "running")   # plain set_state never revokes
    assert _revoked_at(w, b, token_b) is None
