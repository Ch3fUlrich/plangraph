"""M2B-TOKEN-INTEGRITY: run_tokens rows are replayed from events and checked by verify_integrity.

Mutations killed: a raw un-revoke, a deleted or forged row, a rebound row, an expiry past the
24 h cap; the fold's revocation rules drifting from the store's (pinned constant test).
"""
from __future__ import annotations

import sqlite3

from plangraph.keeper import store_tokens, token_fold
from plangraph.keeper.events import verify_integrity

import test_keeper_run_tokens as rt
from test_keeper_run_tokens import claimed_with_token, mint, rp, to_review, w  # noqa: F401

rt.RUNS.setdefault("rvb", "demo:B")       # a reviewer run naming item B
rt.FAMILY.setdefault("rvb", "teamC")


def problems(w):
    return [(p["code"], p["field"]) for p in verify_integrity(w.conn)
            if p["code"].startswith("token_")]


def raw(w, sql, *args):
    conn = sqlite3.connect(str(w.path))
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def test_revocation_rules_match_the_store():
    assert token_fold.REVOKE_ALL == store_tokens._REVOKE_ALL
    assert token_fold.MAX_LIFE_S == store_tokens.RUN_TOKEN_MAX_LIFE_S


def test_honest_lifecycle_verifies_clean(w):
    epoch, tok, p = claimed_with_token(w)
    mint(w, "l1main", "demo:A", "writer", "w1")                 # re-mint revokes the first
    w.store.heartbeat(w.p("l1main"), "demo:A", epoch)
    epoch_b, _, pb = claimed_with_token(w, run="other", key="demo:B")
    w.store.add_evidence(pb, "demo:B", epoch_b, "decision", {"id": "D-1"})
    w.store.submit(pb, "demo:B", epoch_b)                      # revokes writer tokens of B
    rv = rp(w, mint(w, "l1main", "demo:B", "reviewer", "rvb")["token"])
    w.store.verdict(rv, "demo:B", False, "no")                  # changes_requested: all of B
    assert problems(w) == []


def test_raw_unrevoke_is_flagged(w):
    _, tok, p = claimed_with_token(w)
    w.store.release(w.p("l1main"), "demo:A", 1)                 # revokes
    assert problems(w) == []
    raw(w, "UPDATE run_tokens SET revoked_at=NULL")
    assert ("token_mismatch", "revoked") in problems(w)


def test_deleted_forged_and_rebound_rows_are_flagged(w):
    claimed_with_token(w)
    raw(w, "UPDATE run_tokens SET key='demo:B'")
    assert ("token_mismatch", "key") in problems(w)
    raw(w, "INSERT INTO run_tokens(token_sha256,key,project,epoch,role,run_id,created_at,expires_at)"
           " VALUES('x','demo:A','demo',1,'writer','evil','2026-10-09T00:00:00Z','2026-10-09T01:00:00Z')")
    assert ("token_extra", "run_tokens") in problems(w)
    raw(w, "DELETE FROM run_tokens")
    assert ("token_missing", "run_tokens") in problems(w)


def test_expiry_beyond_the_cap_is_flagged(w):
    claimed_with_token(w)
    raw(w, "UPDATE run_tokens SET expires_at='2099-01-01T00:00:00Z'")
    assert ("token_mismatch", "expires_at") in problems(w)


def test_fold_without_table_or_tokens_is_clean(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert token_fold.verify_tokens(conn, []) == []


def test_moving_created_at_to_extend_the_cap_is_flagged(w):
    claimed_with_token(w)
    raw(w, "UPDATE run_tokens SET created_at='2099-01-01T00:00:00Z', expires_at='2099-01-01T23:59:59Z'")
    got = problems(w)
    assert ("token_mismatch", "created_at") in got and ("token_mismatch", "expires_at") in got


def test_a_broken_table_is_not_a_silent_clean(w):
    claimed_with_token(w)
    raw(w, "ALTER TABLE run_tokens RENAME COLUMN revoked_at TO gone")
    import pytest
    with pytest.raises(sqlite3.OperationalError):
        verify_integrity(w.conn)


def test_changed_revocation_time_is_flagged(w):
    claimed_with_token(w)
    w.store.release(w.p("l1main"), "demo:A", 1)
    assert problems(w) == []
    raw(w, "UPDATE run_tokens SET revoked_at='2001-01-01T00:00:00Z'")
    assert ("token_mismatch", "revoked_at") in problems(w)


def test_duplicate_token_id_in_events_is_flagged():
    ev = [{"key": "p:A", "type": "token_minted", "ts": "t1",
           "payload": {"token_id": 1, "role": "writer", "run_id": "r", "epoch": 1}},
          {"key": "p:A", "type": "token_minted", "ts": "t2",
           "payload": {"token_id": 1, "role": "writer", "run_id": "s", "epoch": 1}}]
    assert token_fold.fold_tokens(ev)["__dupes__"] == [1]


def test_swapped_token_hash_is_flagged(w):
    claimed_with_token(w)
    raw(w, "UPDATE run_tokens SET token_sha256='0' || substr(token_sha256, 2)")
    assert ("token_mismatch", "token_sha256") in problems(w)
