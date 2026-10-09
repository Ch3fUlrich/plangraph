"""M2B-OUTBOX: offline queue + keeper replay (dedupe, order, epoch, revocation) and the outage grace.

Mutations killed: a duplicate id executed twice, replay skipping the per-verb checks (stale epoch,
revoked token, reordered submit), a non-run principal replaying, outbox_seen editable, an outage not
extending leases/tokens (or extending without the event the token fold needs), the client writing
the token into the outbox or dropping unanswered entries.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from plangraph.keeper import outage, outbox
from plangraph.keeper.auth import Principal
from plangraph.keeper.errors import Refused
from plangraph.keeper.events import verify_integrity

from test_keeper_run_tokens import claimed_with_token, code, mint, rp, w  # noqa: F401

SYSTEM = Principal("system", "system", [], None)


def eid(n):
    return f"{n:032x}"


def entry(n, verb, epoch, **body):
    return {"id": eid(n), "verb": verb, "body": {"key": "demo:A", "epoch": epoch, **body}}


def results(out):
    return [r["result"] for r in out["results"]]


def events(w, kind):
    return w.conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (kind,)).fetchone()[0]


def test_replay_runs_in_order_and_dedupes(w):
    epoch, tok, p = claimed_with_token(w)
    batch = [entry(1, "heartbeat", epoch),
             entry(2, "add_evidence", epoch, kind="decision", ref={"id": "D-1"}),
             entry(3, "submit", epoch)]
    assert results(w.store.replay(p, batch)) == ["ok", "ok", "ok"]
    assert w.store.show(w.p("reader"), "demo:A")["state"] == "in_review"
    before = events(w, "submitted")
    again = w.store.replay(p, batch)                              # same ids again
    assert results(again) == ["duplicate"] * 3 and again["results"][2]["first"] == "ok"
    assert events(w, "submitted") == before                        # never re-run
    assert verify_integrity(w.conn) == []


def test_reordered_stale_and_revoked_entries_are_refused(w):
    epoch, tok, p = claimed_with_token(w)
    out = w.store.replay(p, [entry(1, "submit", epoch),            # before its evidence
                             entry(2, "heartbeat", epoch + 5)])    # stale epoch
    assert results(out)[0] != "ok" and results(out)[1] in ("epoch_stale", "forbidden")
    w.store.release(w.p("l1main"), "demo:A", epoch)                # revokes the token
    assert results(w.store.replay(p, [entry(3, "heartbeat", epoch)])) == ["forbidden"]


def test_only_run_tokens_replay_and_entries_are_validated(w):
    epoch, tok, p = claimed_with_token(w)
    assert code(w.store.replay, w.p("l1main"), [entry(1, "heartbeat", epoch)]) == "forbidden"
    assert code(w.store.replay, p, []) == "validation"
    assert code(w.store.replay, p, [entry(i, "heartbeat", epoch) for i in range(101)]) == "validation"
    out = w.store.replay(p, [{"id": "nope", "verb": "heartbeat", "body": {}},
                             {"id": eid(9), "verb": "claim", "body": {"key": "demo:A"}}])
    assert results(out) == ["validation", "forbidden"]


def test_outbox_seen_is_append_only(w):
    epoch, tok, p = claimed_with_token(w)
    w.store.replay(p, [entry(1, "heartbeat", epoch)])
    raw = sqlite3.connect(str(w.path))
    for sql in ("UPDATE outbox_seen SET result='x'", "DELETE FROM outbox_seen"):
        with pytest.raises(sqlite3.DatabaseError):
            raw.execute(sql)
    raw.close()


def test_outage_extends_leases_and_tokens_and_verify_follows(w):
    epoch, tok, p = claimed_with_token(w)
    lease0 = w.store.show(w.p("reader"), "demo:A")["lease_until"]
    exp0 = w.conn.execute("SELECT expires_at FROM run_tokens").fetchone()[0]
    now = w.store.now()
    out = w.store.record_outage(SYSTEM, 3600, now - 10, now)
    assert out["leases"] == 1 and out["tokens"] == 1 and events(w, "outage_recorded") == 1
    assert w.store.show(w.p("reader"), "demo:A")["lease_until"] > lease0
    assert w.conn.execute("SELECT expires_at FROM run_tokens").fetchone()[0] > exp0
    assert verify_integrity(w.conn) == []                          # fold replays the grace
    w.clock.advance(7200 + 60)                                     # past the original expiry
    w.store.heartbeat(p, "demo:A", epoch)                          # honoured for the outage
    assert code(w.store.record_outage, w.p("router"), 10, now, now) == "forbidden"


def test_check_and_record_uses_the_alive_stamp(w, tmp_path):
    cfg = SimpleNamespace(db=str(tmp_path / "keeper.db"), reap_interval=60)
    assert outage.check_and_record(w.store, SYSTEM, cfg) is None   # first start: stamp only
    assert outage.alive_path(cfg).exists()
    now = w.store.now()
    os.utime(outage.alive_path(cfg), (now - 30, now - 30))
    assert outage.check_and_record(w.store, SYSTEM, cfg, now=now) is None   # short gap
    os.utime(outage.alive_path(cfg), (now - 900, now - 900))
    assert outage.check_and_record(w.store, SYSTEM, cfg, now=now)["gap_s"] == 900


def test_client_queues_without_the_token_and_replay_keeps_unanswered(tmp_path, monkeypatch, capsys):
    path = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("PG_OUTBOX", str(path))
    monkeypatch.setenv("PG_TOKEN", "pgr1_SECRET")
    assert outbox.on_unreachable("heartbeat", {"key": "demo:A", "epoch": 1}, "down") == 4
    assert outbox.on_unreachable("submit", {"key": "demo:A", "epoch": 1}, "down") == 4
    assert outbox.on_unreachable("claim", {"key": "demo:A"}, "down") == 3   # not queueable
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "pgr1_" not in path.read_text()
    ids = [json.loads(line)["id"] for line in path.read_text().splitlines()]

    class Session:
        def call(self, method, verb, body=None):
            assert verb == "replay"
            return {"results": [{"id": ids[0], "result": "ok"}]}        # only the first answered
    assert outbox.cmd_replay(None, Session()) == 0
    left = [json.loads(line)["id"] for line in path.read_text().splitlines()]
    assert left == ids[1:]
    monkeypatch.delenv("PG_OUTBOX")
    assert outbox.on_unreachable("heartbeat", {}, "down") == 3


def test_outage_gap_is_capped_and_untrusted_stamps_are_ignored(w, tmp_path):
    cfg = SimpleNamespace(db=str(tmp_path / "keeper.db"), reap_interval=60)
    outage.stamp(cfg)
    path = outage.alive_path(cfg)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    now = w.store.now()
    os.utime(path, (0, 0))                                         # an ancient / forged stamp
    assert outage.check_and_record(w.store, SYSTEM, cfg, now=now)["gap_s"] == outage.OUTAGE_MAX_S
    os.utime(path, (now - 900, now - 900))
    os.chmod(path, 0o666)                                          # writable by others: ignored
    assert outage.check_and_record(w.store, SYSTEM, cfg, now=now) is None
    os.utime(path, (now + 3600, now + 3600))                       # from the future: ignored
    assert outage.check_and_record(w.store, SYSTEM, cfg, now=now) is None
    assert w.store.record_outage(SYSTEM, 10 ** 9, now, now)["gap_s"] == 86400


def test_malformed_replay_bodies_are_validation_not_500(w):
    epoch, tok, p = claimed_with_token(w)
    out = w.store.replay(p, [{"id": eid(1), "verb": "add_evidence",
                              "body": {"key": "demo:A", "epoch": "e", "kind": 123, "ref": "x"}},
                             {"id": eid(2), "verb": "heartbeat", "body": "not a dict"}])
    assert all(r in ("validation", "forbidden", "epoch_stale") for r in results(out))
