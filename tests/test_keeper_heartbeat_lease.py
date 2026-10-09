"""PG-HEARTBEAT-LEASE (D-837): a heartbeat extends a lease, never shortens it.

Mutation killed: the old ``lease = now + default`` (no max) cut a 4 h claim lease to
2 h on the first heartbeat, and verify_integrity (cache lease < claimed lease) flagged it.
"""
from __future__ import annotations

import pytest

from keeper_util import World, seed, to_claimed
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _lease(w, key):
    return w.conn.execute("SELECT lease_until FROM items WHERE key=?", (key,)).fetchone()[0]


def test_heartbeat_keeps_a_longer_claim_lease(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key, lease_s=4 * 3600)
    claimed = _lease(w, key)
    w.clock.advance(20 * 60)
    out = w.store.heartbeat(w.p("l1main"), key, epoch)
    assert out["lease_until"] == claimed == _lease(w, key)        # not cut to now + 2 h
    assert w.store.verify_integrity() == []


def test_heartbeat_extends_once_the_default_reaches_past_the_lease(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key, lease_s=4 * 3600)
    claimed = _lease(w, key)
    w.clock.advance(3 * 3600)
    out = w.store.heartbeat(w.p("l1main"), key, epoch)
    assert out["lease_until"] > claimed                              # now + 2 h > claim
    assert w.store.verify_integrity() == []
    w.clock.advance(60)
    again = w.store.heartbeat(w.p("l1main"), key, epoch)["lease_until"]
    assert again >= out["lease_until"]                               # monotonic


def test_default_claim_still_renews_on_heartbeat(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    before = _lease(w, key)
    w.clock.advance(600)
    assert w.store.heartbeat(w.p("l1main"), key, epoch)["lease_until"] > before
    assert w.store.verify_integrity() == []


def test_verify_still_flags_a_shortened_lease(w):
    (key,) = seed(w, [("A", [])])
    to_claimed(w, key, lease_s=4 * 3600)
    import sqlite3
    raw = sqlite3.connect(w.path)
    raw.execute("UPDATE items SET lease_until='2000-01-01T00:00:00Z' WHERE key=?", (key,))
    raw.commit()
    raw.close()
    assert any(p.get("field") == "lease_until" for p in w.store.verify_integrity())


def test_heartbeat_after_reap_is_refused_and_lease_stays_clear(w):
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key, lease_s=3600)
    w.clock.advance(3600 + 5)
    w.store.reap(w.p("system"), w.clock())
    with pytest.raises(Refused):
        w.store.heartbeat(w.p("l1main"), key, epoch)
    assert w.store.verify_integrity() == []


def test_interleaved_heartbeats_never_shorten(w):
    """HTTP requests are serialised by the server lock; any order of beats keeps the max."""
    (key,) = seed(w, [("A", [])])
    epoch = to_claimed(w, key, lease_s=4 * 3600)
    claimed = _lease(w, key)
    seen = []
    for step in (60, 0, 600, 0, 1800):
        w.clock.advance(step)
        seen.append(w.store.heartbeat(w.p("l1main"), key, epoch)["lease_until"])
    assert all(lease == claimed for lease in seen)
    assert w.store.verify_integrity() == []
