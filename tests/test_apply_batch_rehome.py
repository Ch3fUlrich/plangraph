"""apply_batch accepts op ``rehome``: validated locally, applied atomically or ``--per-op``.

The validation tests need no keeper.  The keeper-backed ones are skipped on
hosts where ``plangraph.keeper`` cannot import: it ``fcntl``-flocks its DB and
``fcntl`` is Unix-only (e.g. Windows), where the orchestrator runs them in WSL.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _rel in ("tools", "tests"):
    _path = str(ROOT / _rel)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import apply_batch  # noqa: E402,F401
from apply_batch import BatchError, build_request, validate_op  # noqa: E402

try:
    import fcntl  # noqa: F401
except ImportError:
    KEEPERS = False
else:
    KEEPERS = True

if KEEPERS:
    from keeper_util import defn  # noqa: E402
    from test_apply_batch import run, srv, state_of  # noqa: E402
    from test_apply_batch import token_file, world, write_batch  # noqa: E402,F401

needs_keeper = pytest.mark.skipif(
    not KEEPERS, reason="the keeper needs Unix fcntl (not this host)")

REHOME = {"op": "rehome", "key": "demo:B", "new_project": "plangraph",
          "host": "box"}


def seed_open_item(world):
    """An open lane plus the goal that keeps it reachable."""
    world.store.create_items(world.p("router"), [
        defn("B", [], evidence_kinds=["decision"], review_tier="none"),
        defn("GOAL", ["B"], kind="goal")])


def spy_posts(monkeypatch):
    """Record every ``Session.post`` call, letting it through untouched."""
    calls = []
    real_post = apply_batch.Session.post

    def spy(self, verb, body):
        calls.append((verb, body))
        return real_post(self, verb, body)

    monkeypatch.setattr(apply_batch.Session, "post", spy)
    return calls


def aliases_of(world):
    return [(r["old"], r["new"])
            for r in world.store.aliases(world.p("router"))]


# --------------------------------------------------------------- local veto


def test_rehome_op_validates():
    validate_op(dict(REHOME))
    validate_op({"op": "rehome", "key": "demo:B", "new_project": "moved"})
    verb, body = build_request(REHOME, "plan/batch.md", "f" * 64)
    assert verb == "rehome"
    assert body == {"key": "demo:B", "new_project": "plangraph", "host": "box"}


def test_rehome_request_matches_the_single_route_arguments():
    reply = build_request({"op": "rehome", "key": "demo:B",
                           "new_project": "moved"}, "plan/batch.md", "d")
    assert reply == ("rehome", {"key": "demo:B", "new_project": "moved",
                                "host": None})


def test_rehome_without_new_project_is_batch_error():
    with pytest.raises(BatchError, match="new_project"):
        validate_op({"op": "rehome", "key": "demo:B"})


def test_rehome_empty_new_project_is_batch_error():
    with pytest.raises(BatchError, match="non-empty"):
        validate_op({"op": "rehome", "key": "demo:B", "new_project": ""})


def test_rehome_unknown_field_is_batch_error():
    with pytest.raises(BatchError, match="unknown field"):
        validate_op({**REHOME, "owner": "someone"})


def test_rehome_bad_key_is_batch_error():
    for key in ("novote", "demo", "demo/B", "demo:B extra"):
        with pytest.raises(BatchError, match="project:ID"):
            validate_op({**REHOME, "key": key})


def test_rehome_host_must_be_none_or_a_string():
    validate_op({**REHOME, "host": None})
    with pytest.raises(BatchError, match="host"):
        validate_op({**REHOME, "host": 7})


# ------------------------------------------------------------ keeper-backed


@needs_keeper
def test_atomic_run_sends_one_apply_ops_and_moves_the_item(
        world, srv, token_file, tmp_path, monkeypatch):
    seed_open_item(world)
    calls = spy_posts(monkeypatch)
    rc, out, err = run(write_batch(tmp_path, ops=[REHOME], name="rehome"),
                       srv.base_url, token_file)
    assert rc == 0, out + err
    assert [verb for verb, _ in calls] == ["apply_ops"]
    assert calls[0][1]["ops"] == [REHOME]      # unchanged, no per-op verb
    assert "ok 1 rehome demo:B" in out
    assert "summary: rehome 1 (applied 1 of 1)" in out
    moved = world.store._item("plangraph:B")
    assert moved is not None and moved["state"] == "open"
    assert moved["host"] == "box"
    assert state_of(world, "demo:B") == "cancelled"
    assert aliases_of(world) == [("demo:B", "plangraph:B")]


@needs_keeper
def test_per_op_run_posts_the_single_rehome_route_with_the_same_result(
        world, srv, token_file, tmp_path, monkeypatch):
    seed_open_item(world)
    calls = spy_posts(monkeypatch)
    rc, out, err = run(write_batch(tmp_path, ops=[REHOME],
                                   name="rehome-per-op"),
                       srv.base_url, token_file, extra=["--per-op"])
    assert rc == 0, out + err
    assert [verb for verb, _ in calls] == ["rehome"]
    assert calls[0][1] == {"key": "demo:B", "new_project": "plangraph",
                           "host": "box"}
    assert "ok 1 rehome demo:B" in out
    moved = world.store._item("plangraph:B")
    assert moved is not None and moved["state"] == "open"
    assert moved["host"] == "box"
    assert state_of(world, "demo:B") == "cancelled"
    assert aliases_of(world) == [("demo:B", "plangraph:B")]


@needs_keeper
def test_a_refusing_op_rolls_the_rehome_back(world, srv, token_file, tmp_path):
    seed_open_item(world)
    ops = [REHOME, {"op": "edit", "key": "demo:MISSING",
                    "fields": {"title": "no such item"}}]
    rc, out, err = run(write_batch(tmp_path, ops=ops, name="rehomeback"),
                       srv.base_url, token_file)
    assert rc == 1, out + err
    assert "op 2 edit demo:MISSING: not_found" in out
    assert "nothing applied (the whole batch was rolled back)" in out
    assert "resume with" not in out
    assert world.store._item("plangraph:B") is None
    assert state_of(world, "demo:B") == "open"
    assert aliases_of(world) == []
