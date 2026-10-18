"""F5 stall alarms: ``K/stall.py`` rows merged into ``GET /v1/alarms``.

Two computed kinds: ``stall_no_event`` (a ``claimed`` item whose newest event is
older than 2 hours) and ``merged_open`` (an open item a ``Plan-Item`` trailer on
main already claims).  The unit level calls ``stall_alarms`` with an injected
``git`` (a callable ``(repo, ref) -> [(sha, value)]`` or a repos-config object);
the merge level calls ``routes.read`` directly with fake principals — the HTTP
doors themselves are covered by ``test_inbox_alarms.py``.
"""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from keeper_util import World, defn, to_claimed, to_done
from plangraph.keeper.store import Store
from plangraph.keeper.stall import stall_alarms
from plangraph.inbox import routes
from plangraph.keeper.events import append_event

HOUR = 3600.0


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    from plangraph.keeper import stall
    monkeypatch.setattr(stall, "_CACHE_TTL_S", 0.0)
    stall._cache.clear()


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def event(w, key, ts=None):
    """A heartbeat for *key* stamped *ts* (default: the current clock)."""
    append_event(w.conn, w.store._iso(w.clock() if ts is None else ts),
                 "router", "router", None, key, "heartbeat", {})
    w.conn.commit()


def claimed_demo(w, pid="A"):
    keys = [k for k in _seed(w, [("A", ()), ("B", ())]) if k.endswith(":" + pid)]
    to_claimed(w, keys[0], who="l1main", lease_s=10 * HOUR)
    return keys[0]


def _seed(w, lanes, project="demo"):
    items = [defn(pid, deps, project=project) for pid, deps in lanes]
    items.append(defn("GOAL", [pid for pid, _ in lanes], project=project, kind="goal"))
    w.store.create_items(w.p("router"), items)
    return [f"{project}:{pid}" for pid, _ in lanes]


def keys_of(rows):
    return [r["key"] for r in rows]


def config_with_repo(path, ref="main"):
    config_path = path / "keeper.json"
    config_path.write_text(json.dumps({"repos": {"demo": str(path / "repo")}}),
                           encoding="utf-8")
    os.chmod(config_path, 0o600)
    from plangraph.keeper.config import load_config
    try:
        return load_config(config_path)
    except Exception:      # a strict 600 mode check is not portable (Windows)
        return SimpleNamespace(repos={"demo": str(path / "repo")})


def make_commit(repo, key):
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                    "-m", f"work\n\nPlan-Item: {key}"], cwd=str(repo), check=True,
                   capture_output=True)


# ----------------------------------------------------------- stall_no_event
def test_claimed_item_without_recent_events_alarms(w):
    key = claimed_demo(w, "A")
    event(w, key)
    w.clock.advance(3 * HOUR)
    rows = stall_alarms(w.conn, w.clock())
    assert keys_of(rows) == [key]
    row = rows[0]
    assert row["kind"] == "stall_no_event" and row["workflow_id"] is None
    assert row["raised_at"] == w.store._iso(w.clock() - 3 * HOUR)
    assert "10800" in row["detail"] and "3h" in row["detail"]   # seconds and hour chunks


def test_claimed_item_with_a_recent_event_does_not_alarm(w):
    key = claimed_demo(w, "A")
    event(w, key)
    w.clock.advance(1 * HOUR)
    assert stall_alarms(w.conn, w.clock()) == []


def test_newest_event_decides(w):
    key = claimed_demo(w, "A")
    event(w, key)
    w.clock.advance(3 * HOUR)
    event(w, key)                                # newest heartbeat, inside 2h
    assert stall_alarms(w.conn, w.clock()) == []
    w.clock.advance(7201)                        # newest now 7201s: past 2h
    rows = stall_alarms(w.conn, w.clock())
    assert keys_of(rows) == [key]
    assert rows[0]["raised_at"] == w.store._iso(w.clock() - 7201)


def test_a_done_item_never_alarms(w):
    key = _seed(w, [("A", ())])[0]
    to_done(w, key)
    w.clock.advance(5 * HOUR)
    assert stall_alarms(w.conn, w.clock()) == []


# ------------------------------------------------------------------ merged_open
def test_merged_trailer_on_main_alarms_an_open_item(w, git_repo):
    repo, key = git_repo
    git = SimpleNamespace(repos={"demo": str(repo)})
    rows = stall_alarms(w.conn, w.clock(), git=git)
    assert [(r["kind"], r["key"]) for r in rows] == [("merged_open", key)]
    assert rows[0]["workflow_id"] is None and rows[0]["detail"].startswith("merged commit ")
    assert rows[0]["raised_at"]                           # a real ISO stamp


def test_merged_trailer_via_injected_git_callable(w):
    _seed(w, [("A", ())])
    rows = stall_alarms(w.conn, w.clock(), git=lambda repo, ref: [("deadbeef", "demo:A")])
    assert [(r["kind"], r["key"]) for r in rows] == [("merged_open", "demo:A")]


def test_a_closed_or_other_project_item_does_not_alarm(w):
    done = _seed(w, [("A", ()), ("B", ())])[0]
    to_done(w, done)
    git = lambda repo, ref: [("sha", done), ("sha", "demo:B"), ("sha", "other:C")]
    kinds = [(r["kind"], r["key"]) for r in stall_alarms(w.conn, w.clock(), git=git)]
    assert kinds == [("merged_open", "demo:B")]


def test_no_repo_or_a_failing_git_is_skipped_silently(w):
    key = claimed_demo(w, "A")
    event(w, key)
    w.clock.advance(3 * HOUR)
    for git in (None, SimpleNamespace(), SimpleNamespace(repos={}),
                SimpleNamespace(repos={"demo": "Z:/no-such-repo"})):
        rows = stall_alarms(w.conn, w.clock(), git=git)
        assert keys_of(rows) == [key]                    # stall rows only, no crash


# ------------------------------------------------------------ merged into GET
def listed(w, who):
    return routes.read(w.store, w.p(who), "alarms", {})["alarms"]


def test_routes_merges_stall_rows_with_the_same_visibility(w):
    stall = claimed_demo(w, "A")                        # demo project
    other = _seed(w, [("C", ())], project="other")[0]
    to_claimed(w, other, who="l1other", lease_s=10 * HOUR)
    event(w, other)
    w.clock.advance(3 * HOUR)
    w.conn.execute(
        "INSERT INTO projector_alarms(id, kind, workflow_id, key, detail, raised_at)"
        " VALUES(9, 'state_drift', 'wf', 'demo:Z', 'drifted', ?)",
        (w.store._iso(w.clock()),))
    w.conn.commit()
    rows = listed(w, "router")
    assert [(r["id"], r["key"], r["kind"]) for r in rows] == [
        (9, "demo:Z", "state_drift"), (None, stall, "stall_no_event"),
        (None, other, "stall_no_event")]
    assert [r["age_s"] for r in rows] == [0, int(3 * HOUR), int(3 * HOUR)]
    assert set(rows[0]) == {"id", "key", "kind", "workflow_id", "detail",
                            "raised_at", "age_s"}
    assert keys_of(listed(w, "l1main")) == ["demo:Z", stall]
    assert keys_of(listed(w, "l1other")) == [other]
    assert keys_of(listed(w, "reviewer2")) == ["demo:Z", stall]   # a demo reviewer
    assert keys_of(listed(w, "reader")) == ["demo:Z", stall, other]  # _SEES_ALL: stored first


def test_routes_merges_merged_open_rows(w):
    _seed(w, [("A", ()), ("B", ())])
    git = lambda repo, ref: [("sha", "demo:A"), ("sha", "demo:B")]
    w.store.config = SimpleNamespace(repos={"demo": git})
    try:
        assert keys_of(listed(w, "reader")) == ["demo:A", "demo:B"]
        assert keys_of(listed(w, "l1other")) == []
    finally:
        del w.store.config


@pytest.fixture
def git_repo(tmp_path, w):
    keys = _seed(w, [("A", ())])
    repo = tmp_path / "repo"
    make_commit(repo, keys[0])
    return repo, keys[0]


def test_git_is_cached_between_reads_and_scoped_to_its_project(w, git_repo, monkeypatch):
    from plangraph.keeper import stall
    repo, key = git_repo
    monkeypatch.setattr(stall, "_CACHE_TTL_S", 60.0)
    calls = []
    real = stall.cli_check.plan_item_trailers
    monkeypatch.setattr(stall.cli_check, "plan_item_trailers",
                        lambda r, ref: calls.append(r) or real(r, ref))
    git = SimpleNamespace(repos={"demo": str(repo)})
    assert len(stall_alarms(w.conn, w.clock(), git=git)) == 1
    assert len(stall_alarms(w.conn, w.clock(), git=git)) == 1
    assert len(calls) == 1                        # second read served from the cache
    stall._cache.clear()
    other = SimpleNamespace(repos={"other": str(repo)})   # repo of another project
    assert stall_alarms(w.conn, w.clock(), git=other) == []
