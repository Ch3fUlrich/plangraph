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

import sys
KEEPERS = sys.platform != "win32"

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

    def spy(self, verb, body, timeout=None):
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


# ------------------------------------ KEEPER-PROJECT-SCOPED-L1-APPLY (D-6390)
def _write_l1_batch(tmp_path, *, principal="l1main", name="l1-batch",
                    source="docs/plan/basic.md", ops=None):
    import hashlib
    import yaml
    path = tmp_path / f"{name}.yaml"
    plan = tmp_path / source
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# Fixture plan\n", encoding="utf-8")
    doc = {"batch": name, "source": source, "principal": principal,
           "source_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
           "ops": ops or []}
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _token_for(world, tmp_path, name):
    tf = tmp_path / f"{name}.token"
    tf.write_text(world.tokens[name], encoding="utf-8")
    return str(tf)


@needs_keeper
def test_apply_batch_accepts_granted_l1_and_writes_routing_inbox(
        world, srv, tmp_path):
    import hashlib
    world.store.create_items(world.p("router"), [
        defn("B", [], evidence_kinds=["decision"], review_tier="none"),
        defn("GOAL", ["B"], kind="goal")])
    world.store.grant_apply(world.p("router"), "l1main", "demo")
    batch_path = _write_l1_batch(
        tmp_path, principal="l1main", name="granted-run-1",
        source="docs/plan/item.md",
        ops=[{"op": "create", "item": {
            "project": "demo", "id": "C1", "title": "created by l1",
            "track": "T0", "owner": "o", "depends_on": ["demo:B"],
            "under": "demo:GOAL", "done_criteria": "done"}},
             {"op": "edit", "key": "demo:B",
              "fields": {"title": "edited by l1"}}])
    expected_sha = hashlib.sha256(batch_path.read_bytes()).hexdigest()
    l1_tf = _token_for(world, tmp_path, "l1main")
    rc, out, err = run(batch_path, srv.base_url, l1_tf)
    assert rc == 0, out + err
    assert world.store._item("demo:C1")["title"] == "created by l1"
    assert world.store._item("demo:B")["title"] == "edited by l1"
    inbox = [m for m in world.store.messages(world.p("router"))
             if m["to"] == "routing"]
    assert len(inbox) == 1
    body = inbox[0]["body"]
    assert body["batch"] == "granted-run-1"
    assert body["batch_sha256"] == expected_sha
    assert body["op_count"] == 2
    assert body["source"] == "docs/plan/item.md"
    assert "\n" not in body["text"]


@needs_keeper
def test_apply_batch_refuses_ungranted_l1_per_op_and_foreign_key(
        world, srv, tmp_path):
    world.store.create_items(world.p("router"), [
        defn("B", [], evidence_kinds=["decision"], review_tier="none"),
        defn("GOAL", ["B"], kind="goal")])
    world.store.create_items(world.p("router"), [
        {"project": "other", "id": "OG", "title": "other g", "track": "T0",
         "owner": "o", "kind": "goal", "review_tier": "standard",
         "done_criteria": "goal"}])
    world.store.create_items(world.p("router"), [
        {"project": "other", "id": "OB", "title": "foreign b", "track": "T0",
         "owner": "o", "under": "other:OG", "done_criteria": "done"}])
    # Ungranted l1 refused
    l1_tf = _token_for(world, tmp_path, "l1main")
    p_ungranted = _write_l1_batch(
        tmp_path, principal="l1main", name="ungranted",
        ops=[{"op": "edit", "key": "demo:B", "fields": {"title": "x"}}])
    rc, out, err = run(p_ungranted, srv.base_url, l1_tf)
    assert rc == 1, out + err

    # Granted l1: --per-op refused, foreign key refused + rolled back
    world.store.grant_apply(world.p("router"), "l1main", "demo")
    rc_po, out_po, _ = run(p_ungranted, srv.base_url, l1_tf, extra=["--per-op"])
    assert rc_po == 1, out_po

    p_foreign = _write_l1_batch(
        tmp_path, principal="l1main", name="foreign-k",
        ops=[{"op": "edit", "key": "demo:B", "fields": {"title": "rolled"}},
             {"op": "edit", "key": "other:OB", "fields": {"title": "x"}}])
    rc_fk, out_fk, err_fk = run(p_foreign, srv.base_url, l1_tf)
    assert rc_fk == 1, out_fk + err_fk
    assert "other:OB" in out_fk + err_fk
    assert world.store._item("demo:B")["title"] != "rolled"
    assert world.store.messages(world.p("router")) == []


@needs_keeper
def test_mcp_apply_ops_accepts_granted_l1_and_refuses_foreign_key(
        world, srv, tmp_path):
    from plangraph.keeper import mcp
    world.store.create_items(world.p("router"), [
        defn("B", [], evidence_kinds=["decision"], review_tier="none"),
        defn("GOAL", ["B"], kind="goal")])
    l1_tf = _token_for(world, tmp_path, "l1main")
    client = mcp.HttpKeeper(srv.base_url, str(l1_tf))

    # Ungranted fails
    res0 = mcp._call_tool(1, {"name": "apply_ops", "arguments": {
        "ops": [{"op": "priority", "key": "demo:B", "priority": 6}]
    }}, client)["result"]
    assert res0["isError"] is True

    # Granted succeeds within demo and writes routing message
    world.store.grant_apply(world.p("router"), "l1main", "demo")
    res1 = mcp._call_tool(2, {"name": "apply_ops", "arguments": {
        "ops": [{"op": "priority", "key": "demo:B", "priority": 6}]
    }}, client)["result"]
    assert res1["isError"] is False
    assert world.store._item("demo:B")["priority"] == 6
    assert len([m for m in world.store.messages(world.p("router"))
                if m["to"] == "routing"]) == 1

    # Foreign key fails via MCP and rolls back
    res2 = mcp._call_tool(3, {"name": "apply_ops", "arguments": {
        "ops": [{"op": "priority", "key": "demo:B", "priority": 1},
                {"op": "priority", "key": "other:B", "priority": 1}]
    }}, client)["result"]
    assert res2["isError"] is True
    assert "other:B" in res2["content"][0]["text"]
    assert world.store._item("demo:B")["priority"] == 6
