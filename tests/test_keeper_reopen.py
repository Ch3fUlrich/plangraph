"""PG-REOPEN: router-only ``in_review -> claimed`` at a fresh work epoch.

Mutations killed: a non-router caller allowed, a reason that is missing or
unbounded, a decision that is not recorded (or malformed), a reopen from any
state but ``in_review``, an old-epoch submit or close accepted after the
reopen, the fold disagreeing with the cache, and the live run tokens of the
item left unrevoked.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from keeper_util import (FakeVerifier, World, defn, to_claimed, to_done,
                         to_in_review)
from plangraph.keeper import auth, store_tokens, token_fold
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.events import verify_integrity

KEY = "demo:ISSUE"


class _Runs:
    """A run record that knows one writer run, for the token test."""

    def valid_run_id(self, run_id):
        return isinstance(run_id, str) and run_id.replace("-", "").isalnum()

    def job(self, run_id):
        if run_id != "w1":
            raise FileNotFoundError(run_id)
        return {"request": {"title": f"work on {KEY}", "item_key": KEY}, "task": ""}


def _decisions(tmp_path, body):
    path = tmp_path / "DECISIONS.md"
    path.write_text("| ID | Note | Scope |\n|---|---|---|\n" + body, encoding="utf-8")
    return DecisionsAdapter(path)


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.store.create_items(world.p("router"), [
        defn("ISSUE", evidence_kinds=("decision",), done_criteria="upstream issue filed"),
        defn("RUN", evidence_kinds=("semaphore-task",), done_criteria="semaphore run green"),
        defn("CODE", evidence_kinds=("commit+ci",), done_criteria="merged; CI green; 2 seats"),
        defn("DEC", evidence_kinds=("decision", "commit"), review_tier="none"),
        defn("G", ["ISSUE", "RUN", "CODE", "DEC"], kind="goal")])
    world.store.ctx = SimpleNamespace(
        decisions=_decisions(
            tmp_path,
            "| D-900 | closes | Closes: demo:ISSUE demo:RUN demo:CODE |\n"
            "| D-901 | other | Closes: demo:OTHER |\n"
            "| D-902 | reopen | Closes: demo:DEC |\n"),
        runs=_Runs())
    yield world
    world.close()


def _row(w, key):
    return w.conn.execute(
        "SELECT state, epoch, round, claimed_by, delegate, reviewer,"
        " review_waiver, lease_until FROM items WHERE key=?", (key,)).fetchone()


# --------------------------------------------------------------- happy path
def test_router_reopens_to_the_claimant_at_a_fresh_epoch(w):
    epoch = to_in_review(w, KEY)
    out = w.store.reopen(w.p("router"), KEY, "review seats were wrong", "D-900")
    assert (out["state"], out["epoch"], out["claimed_by"]) == ("claimed", epoch + 1, "l1main")
    row = _row(w, KEY)
    assert (row["state"], row["epoch"], row["round"]) == ("claimed", epoch + 1, 0)
    assert row["claimed_by"] == "l1main" and row["reviewer"] is None
    assert row["review_waiver"] is None and row["lease_until"] == out["lease_until"]
    assert verify_integrity(w.conn) == []
    ev = w.conn.execute("SELECT payload FROM events WHERE key=?"
                        " AND type='item_reopened'", (KEY,)).fetchone()
    assert ev is not None and '"D-900"' in ev["payload"]
    assert "review seats were wrong" in ev["payload"]


def test_a_500_character_reason_is_accepted(w):
    epoch = to_in_review(w, KEY)
    out = w.store.reopen(w.p("router"), KEY, "r" * 500, "D-900")
    assert out["state"] == "claimed" and out["epoch"] == epoch + 1


def test_a_recorded_decision_needs_no_closes_naming(w):
    """Unlike ``withdraw_review``, reopen is keyed on the reason, not on Closes:"""
    to_in_review(w, KEY)
    out = w.store.reopen(w.p("router"), KEY, "wrong lane picked it up", "D-901")
    assert out["state"] == "claimed"


# --------------------------------------------------------------- authorisation
@pytest.mark.parametrize("who", ["l1main", "l1other", "reviewer", "workerA", "reader", "system"])
def test_only_the_router_may_reopen(w, who):
    to_in_review(w, KEY)
    with pytest.raises(Refused) as exc:
        w.store.reopen(w.p(who), KEY, "a reason", "D-900")
    assert exc.value.code == "forbidden"
    assert _row(w, KEY)["state"] == "in_review"


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("reason", ["", "   ", None, 7, "x" * 501])
def test_reason_must_be_a_bounded_non_empty_string(w, reason):
    to_in_review(w, KEY)
    with pytest.raises(Refused) as exc:
        w.store.reopen(w.p("router"), KEY, reason, "D-900")
    assert exc.value.code == "validation"
    assert _row(w, KEY)["state"] == "in_review"


def test_decision_must_be_recorded(w):
    to_in_review(w, KEY)
    for did, code in (("D-999", "evidence_unverified"), ("D-90x", "validation"),
                      ("not-a-d", "validation"), ("", "validation"),
                      (None, "validation")):
        with pytest.raises(Refused) as exc:
            w.store.reopen(w.p("router"), KEY, "a reason", did)
        assert exc.value.code == code
    assert _row(w, KEY)["state"] == "in_review"


# ------------------------------------------------------------------- bad state
def test_only_from_in_review(w):
    with pytest.raises(Refused) as exc:                                # open
        w.store.reopen(w.p("router"), KEY, "a reason", "D-900")
    assert exc.value.code == "bad_state"


def test_a_claimed_item_cannot_be_reopened(w):
    to_claimed(w, KEY)
    with pytest.raises(Refused) as exc:
        w.store.reopen(w.p("router"), KEY, "a reason", "D-900")
    assert exc.value.code == "bad_state"
    assert _row(w, KEY)["state"] == "claimed"


def test_a_done_item_cannot_be_reopened(w):
    to_done(w, KEY)
    with pytest.raises(Refused) as exc:
        w.store.reopen(w.p("router"), KEY, "a reason", "D-900")
    assert exc.value.code == "bad_state"
    assert _row(w, KEY)["state"] == "done"


# -------------------------------------------------------- the epoch really moved
def test_submit_of_the_old_epoch_is_refused(w):
    epoch = to_in_review(w, KEY)
    out = w.store.reopen(w.p("router"), KEY, "redo the work", "D-900")
    assert out["epoch"] == epoch + 1
    with pytest.raises(Refused) as exc:
        w.store.submit(w.p("l1main"), KEY, epoch)
    assert exc.value.code == "epoch_stale"
    # the old epoch's evidence does not satisfy the new one either
    with pytest.raises(Refused) as exc:
        w.store.submit(w.p("l1main"), KEY, out["epoch"])
    assert exc.value.code == "evidence_missing"
    assert verify_integrity(w.conn) == []


def test_close_without_fresh_evidence_for_the_new_epoch_is_refused(w, tmp_path):
    key = "demo:DEC"
    w.store.verifiers["commit"] = FakeVerifier(ok=True)   # X7b router-only kind
    root = tmp_path / "plan"
    root.mkdir()
    batch = root / "batch.md"
    batch.write_text("batch body\n", encoding="utf-8")
    w.store.batch_roots = [str(root)]
    batch_ref = f"{batch}@{hashlib.sha256(batch.read_bytes()).hexdigest()}"

    epoch = to_claimed(w, key)
    w.store.add_evidence_router(w.p("router"), key, "decision", {"id": "D-902"})
    w.store.add_evidence_router(w.p("router"), key, "commit", {"sha": "a" * 40})
    w.store.submit(w.p("l1main"), key, epoch)

    out = w.store.reopen(w.p("router"), key, "commit never landed", "D-902")
    assert out["state"] == "claimed" and out["epoch"] == epoch + 1
    assert verify_integrity(w.conn) == []
    # a fresh decision row proves the refusal below is about the stale commit
    w.store.add_evidence_router(w.p("router"), key, "decision", {"id": "D-902"})
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), key, batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert exc.value.msg == "no commit evidence"
    assert w.store._item(key)["state"] == "claimed"
    # the new epoch closes once the commit evidence is re-attached
    w.store.add_evidence_router(w.p("router"), key, "commit", {"sha": "b" * 40})
    assert w.store.close_on_decision(w.p("router"), key, batch_ref)["state"] == "done"
    assert verify_integrity(w.conn) == []


# ---------------------------------------------------------------------- tokens
def test_reopen_revokes_live_tokens_of_the_item():
    assert "item_reopened" in store_tokens._REVOKE_ALL
    assert store_tokens._REVOKE_ALL == token_fold.REVOKE_ALL


def test_a_live_writer_token_is_revoked_by_the_reopen(w):
    epoch = to_claimed(w, KEY)
    token = w.store.mint_run_token(w.p("l1main"), KEY, "writer", "w1")["token"]
    writer = auth.authenticate(w.conn, token)
    w.store.add_evidence(w.p("l1main"), KEY, epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1main"), KEY, epoch)
    w.store.reopen(w.p("router"), KEY, "redo the work", "D-900")
    row = w.conn.execute("SELECT revoked_at FROM run_tokens WHERE token_sha256=?",
                         (auth.hash_token(token),)).fetchone()
    assert row["revoked_at"] is not None
    with pytest.raises(Refused) as exc:
        w.store.heartbeat(writer, KEY, epoch)
    assert exc.value.code == "forbidden"
    assert verify_integrity(w.conn) == []
