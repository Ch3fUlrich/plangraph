"""Hermetic keeper-backed stall alert checks (no HTTP or fleet endpoints)."""
import time
from datetime import UTC, datetime

from keeper_util import defn

from plangraph.keeper.crit_path_stall import run


class Reads:
    def __init__(self, world):
        self.world = world

    def call(self, method, verb, params=None):
        assert method == "GET"  # V5: no keeper mutations
        store, principal = self.world.store, self.world.p("reader")
        if verb == "snapshot":
            return store.snapshot(principal)
        assert verb == "show"
        return store.show(principal, params["key"])


def fixture(w):
    w.store.create_items(w.p("router"), [
        defn("young"), defn("mid"), defn("old"), defn("done"),
        defn("off"), defn("bridge", ["old"]),
        defn("target", ["young", "mid", "bridge", "done"], kind="goal"),
        defn("other", ["off"], kind="goal"),
    ])
    for key, age, state in [("young", 11, "claimed"),
                            ("mid", 13, "claimed"),
                            ("old", 25, "in_review"),
                            ("done", 25, "done"),
                            ("off", 25, "claimed")]:
        # Fixture state rows plus real keeper events provide exact age boundaries.
        at = w.clock() - age * 3600
        with w.store._write():
            w.store._append(w.p("router"), f"demo:{key}", "fixture",
                            {"state": state}, at)
            w.conn.execute("UPDATE items SET state=? WHERE key=?",
                           (state, f"demo:{key}"))
    return Reads(w)


def test_scope_thresholds_repeat_activity_and_no_write(w, tmp_path):
    session = fixture(w)
    path = tmp_path / "state.json"
    sent = []
    def invoke(**kw):
        return run(session, path, lambda *args: sent.append(args),
                   target="demo:target", now=w.clock(), **kw)
    before = list(w.conn.execute("SELECT seq, hash FROM events"))
    lines = invoke()
    assert len(lines) == 3  # 13h: first; 25h: both thresholds
    assert all("demo:mid" in line or "demo:old" in line for line in lines)
    assert len(sent) == 6
    assert {r for r, _ in sent} == {"tester", "routing"}
    assert invoke() == []
    assert list(w.conn.execute("SELECT seq, hash FROM events")) == before
    with w.store._write():
        w.store._append(w.p("router"), "demo:mid", "heartbeat", {}, w.clock())
    assert invoke() == []
    w.clock.advance(13 * 3600)
    assert any("demo:mid" in line for line in invoke())


def test_dry_run_and_configurable_thresholds(w, tmp_path):
    session = fixture(w)
    path = tmp_path / "absent" / "state.json"
    def forbidden(*args):
        raise AssertionError("dry-run sent a message")
    lines = run(session, path, forbidden, target="demo:other",
                thresholds=(10 * 3600, 30 * 3600), dry_run=True, now=w.clock())
    assert len(lines) == 1 and "demo:off" in lines[0]
    assert not path.parent.exists()


def test_state_change_resets_episode(w, tmp_path):
    session = fixture(w)
    path = tmp_path / "state.json"
    def invoke():
        return run(session, path, lambda *args: None,
                   target="demo:target", now=w.clock())
    invoke()
    w.conn.execute("UPDATE items SET state='in_review' WHERE key='demo:mid'")
    assert len(invoke()) == 1
    assert invoke() == []


def test_exact_threshold_is_not_over(w, tmp_path):
    session = fixture(w)
    at = datetime.fromtimestamp(w.clock() - 12 * 3600, UTC).isoformat()
    original = session.call
    def call(method, verb, params=None):
        result = original(method, verb, params)
        if verb == "show":
            result["last_activity"]["at"] = at
        return result
    session.call = call
    assert run(session, tmp_path / "state", lambda *a: None,
                target="demo:target", now=w.clock()) == []


def test_naive_activity_is_utc_in_non_utc_host(w, tmp_path, monkeypatch):
    session = fixture(w)
    original = session.call
    at = datetime.fromtimestamp(w.clock() - 13 * 3600, UTC)
    def call(method, verb, params=None):
        result = original(method, verb, params)
        if verb == "show":
            result["last_activity"]["at"] = at.replace(tzinfo=None).isoformat()
        return result
    session.call = call
    try:
        with monkeypatch.context() as patch:
            patch.setenv("TZ", "EST5")
            time.tzset()
            lines = run(session, tmp_path / "state", lambda *a: None,
                        target="demo:other", now=w.clock())
            assert len(lines) == 1
    finally:
        time.tzset()


def test_threshold_changes_do_not_replay_levels(w, tmp_path):
    session = fixture(w)
    sent = []
    def invoke(thresholds):
        return run(session, tmp_path / "state", lambda *a: sent.append(a),
                   target="demo:other", thresholds=thresholds, now=w.clock())
    assert len(invoke((12 * 3600, 24 * 3600))) == 2
    assert invoke((10 * 3600, 20 * 3600)) == []
    assert len(sent) == 4


def test_candidate_reentry_does_not_replay_episode(w, tmp_path):
    session = fixture(w)
    original = session.call
    visible = True
    def call(method, verb, params=None):
        result = original(method, verb, params)
        if verb == "snapshot" and not visible:
            for item in result["items"]:
                if item["key"] == "demo:other":
                    item["depends_on"] = []
        return result
    session.call = call
    def invoke():
        return run(session, tmp_path / "state", lambda *a: None,
                   target="demo:other", now=w.clock())
    assert len(invoke()) == 2
    visible = False
    assert invoke() == []
    visible = True
    assert invoke() == []
