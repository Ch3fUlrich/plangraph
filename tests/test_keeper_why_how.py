"""Why/how enforcement uses a hermetic SQLite keeper, not a live service."""
from types import SimpleNamespace

import pytest

from keeper_util import World, defn
from plangraph import views
from plangraph.keeper import brief, fmt
from plangraph.keeper.brief import missing_why_how
from plangraph.keeper.cli_check import cmd_check
from plangraph.keeper.cli_verbs import cmd_define
from plangraph.keeper.errors import Refused
from plangraph.keeper.mcp import HttpKeeper, handle
import json


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path, backend="sqlite")
    yield world
    world.close()


@pytest.mark.parametrize("kind", ["goal", "decision"])
@pytest.mark.parametrize("description, missing", [
    (None, "Why: and How:"), ("short prose", "Why: and How:"),
    ("How: implement", "Why:"), ("Why: useful", "How:"),
    ("Why:   \nHow: implement", "Why:"),
    ("why: useful\nHow: implement", "Why:"),
    ("prefix Why: useful\nHow: implement", "Why:"),
    ("Why:\nHow: implement", "Why:"),
    ("Why: useful\nHow: \t", "How:"),
    ("Why: \nHow: \t", "Why: and How:"),
])
def test_front_doors_refuse_missing_sections(w, tmp_path, capsys, kind, description, missing):
    items = [defn("G", kind=kind, description=description)]
    result = handle({"id": 1, "method": "tools/call", "params": {
        "name": "create", "arguments": {"items": items}}},
        SimpleNamespace(call=lambda *args: ("created", False)))["result"]
    assert result["isError"]
    assert result["content"][0]["text"] == (
        f"missing_why_how: G: description missing {missing}")
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items))
    assert cmd_define(SimpleNamespace(file=path, edit=False), object()) == 2
    error = capsys.readouterr().err
    assert "missing_why_how" in error and missing in error
    if kind != "decision":
        assert w.store.create_items(w.p("router"), items)["created"] == ["demo:G"]


@pytest.mark.parametrize("kind, description", [
    ("goal", "Why: useful\nHow: build"),
    ("decision", "Why: useful\nHow: build")])
def test_front_doors_accept_sections_or_decisions(tmp_path, kind, description):
    items = [defn("G", kind=kind, description=description)]
    calls = []
    def call(*args):
        calls.append(args)
        return "created", False
    client = SimpleNamespace(call=call)
    result = handle({"id": 1, "method": "tools/call", "params": {
        "name": "create", "arguments": {"items": items}}}, client)["result"]
    assert not result["isError"] and len(calls) == 1
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items))
    client.call = lambda *args, **kwargs: {"created": ["demo:G"]}
    client.json_mode = False
    assert cmd_define(SimpleNamespace(file=path, edit=False), client) == 0


def test_create_accepts_sections(w):
    result = w.store.create_items(w.p("router"), [
        defn("G", kind="goal", description="Why: useful\nHow:\nimplement")])
    assert result["created"] == ["demo:G"]


def test_cli_edit_does_not_enforce_sections(tmp_path):
    path = tmp_path / "edit.json"
    path.write_text(json.dumps({"description": "legacy prose"}))
    calls = []
    def call(method, verb, **kwargs):
        calls.append((verb, kwargs["body"]))
        return {}
    session = SimpleNamespace(call=call, json_mode=True)
    assert cmd_define(SimpleNamespace(file=path, edit=True, key="demo:G"), session) == 0
    assert calls == [("edit_definition", {
        "key": "demo:G", "fields": {"description": "legacy prose"}})]


def test_decision_creation_is_exempt(w):
    # The exemption is legacy-only; new decisions use the ordinary section rule.
    result = w.store.import_items(w.p("router"), [
        defn("D", kind="decision"),
        defn("G", ["D"], kind="goal", description="Why: useful\nHow: build")])
    assert result["created"] == ["demo:D", "demo:G"]
    assert w.store.show(w.p("reader"), "demo:D")["description"] is None
    w.store.edit_definition(w.p("router"), "demo:D", {"description": "old prose"})
    w.store.apply_ops(w.p("router"), [
        {"op": "edit", "key": "demo:D", "fields": {"description": "still legacy"}}])
    assert w.store.show(w.p("reader"), "demo:D")["description"] == "still legacy"


@pytest.mark.parametrize("path", ["single", "batch", "batch_list"])
@pytest.mark.parametrize("description, missing", [
    (None, "Why: and How:"), ("old prose", "Why: and How:"),
    ("Why: useful", "How:"), ("How: build", "Why:"),
    ("Why: \nHow: build", "Why:"), ("Why: useful\nHow: \t", "How:"),
])
def test_new_decision_storage_refuses_missing_sections(w, path, description, missing):
    goal = defn("G", ["D"], kind="goal")
    decision = defn("D", kind="decision", description=description)
    with pytest.raises(Refused) as exc:
        if path == "single":
            w.store.create_items(w.p("router"), [goal, decision])
        else:
            ops = ([{"op": "create", "item": [goal, decision]}]
                   if path == "batch_list" else [
                       {"op": "create", "item": goal},
                       {"op": "create", "item": decision}])
            w.store.apply_ops(w.p("router"), ops)
    assert exc.value.code == "missing_why_how"
    assert exc.value.msg.endswith(f"demo:D: description missing {missing}")
    assert w.store._all_items() == []
    assert w.conn.execute("SELECT COUNT(*) FROM events WHERE key IS NOT NULL").fetchone()[0] == 0


@pytest.mark.parametrize("path", ["single", "batch", "batch_list"])
@pytest.mark.parametrize("description", [
    "Why: useful\nHow: build", "Why:\nuseful\nHow:\nbuild"])
def test_new_decision_storage_accepts_sections(w, path, description):
    w.store.create_items(w.p("router"), [defn("G", kind="goal")])
    items = [defn("D", kind="decision", description=description, under="G")]
    if path == "single":
        result = w.store.create_items(w.p("router"), items)
    else:
        result = w.store.apply_ops(w.p("router"), [
            {"op": "create", "item": items if path == "batch_list" else items[0]}
        ])["results"][0]
    assert result["created"] == ["demo:D"]
    assert w.store.show(w.p("reader"), "demo:D")["description"] == description


@pytest.mark.parametrize("kind", ["lane", "gate", "goal", "question", "operator_task"])
def test_nondecision_storage_create_remains_permissive(w, kind):
    items = [defn("N", kind=kind)]
    if kind != "goal":
        items.append(defn("G", ["N"], kind="goal"))
    assert w.store.apply_ops(w.p("router"), [
        {"op": "create", "item": items}])["applied"] == 1


def test_mcp_edit_existing_decision_does_not_enforce_sections():
    calls = []
    client = SimpleNamespace(call=lambda *args: (calls.append(args) or "edited", False))
    result = handle({"id": 1, "method": "tools/call", "params": {
        "name": "edit", "arguments": {
            "key": "demo:D", "fields": {"description": "legacy prose"}}}}, client)["result"]
    assert not result["isError"] and calls[0][:2] == ("POST", "edit_definition")


@pytest.mark.parametrize("tool, verb", [("create", "create_items"), ("apply_ops", "apply_ops")])
def test_http_and_mcp_decision_create_enforce_sections(w, tmp_path, tool, verb):
    from door_util import Doors
    doors = Doors(w, tmp_path)
    token = tmp_path / "router.token"
    token.write_text(w.tok("router"))
    client = HttpKeeper(doors.url, str(token))
    items = [defn("D", kind="decision"), defn("G", ["D"], kind="goal")]
    body = ({"items": items} if tool == "create" else
            {"ops": [{"op": "create", "item": items}]})
    try:
        status, error = doors.http("router", "POST", f"/v1/{verb}", body)
        assert status == 400 and error["error"] == "missing_why_how"
        result = handle({"id": 1, "method": "tools/call", "params": {
            "name": tool, "arguments": body}}, client)["result"]
        assert result["isError"]
        assert "missing_why_how:" in result["content"][0]["text"]
        assert w.store._all_items() == []
        for item in items:
            item["description"] = "Why: useful\nHow: build"
        result = handle({"id": 2, "method": "tools/call", "params": {
            "name": tool, "arguments": body}}, client)["result"]
        assert not result["isError"]
        assert w.store.show(w.p("reader"), "demo:D")["description"] == items[0]["description"]
    finally:
        doors.close()


def test_legacy_items_validate_and_edit(w):
    w.store.import_items(w.p("router"), [defn("G", kind="goal")])
    assert views.validate(w.store._all_items()) == []
    w.store.edit_definition(w.p("router"), "demo:G", {"title": "Updated"})
    w.store.edit_definition(w.p("router"), "demo:G", {"description": "old prose"})
    assert views.validate(w.store._all_items()) == []


def test_missing_list_excludes_terminal_items():
    items = [{"key": key, "state": state, "description": desc}
             for key, state, desc in [
                 ("z", "claimed", "How: build"), ("a", "open", None),
                 ("d", "done", None), ("c", "cancelled", None),
                 ("ok", "open", "Why: needed\nHow: build")]]
    assert missing_why_how(items) == ["a", "z"]


def test_check_warns_without_strict_failure(capsys):
    items = [{"key": f"p:{i:02}", "state": "open"} for i in range(25)]
    session = SimpleNamespace(local_items=lambda: items, json_mode=False)
    assert cmd_check(SimpleNamespace(strict=True), session) == 0
    out = capsys.readouterr().out
    assert "missing_why_how: 25 items" in out
    assert "p:19" in out and "p:20" not in out


def test_show_and_brief_display_sections():
    item = {"key": "p:G", "state": "open", "kind": "goal",
            "description": "intro " * 150 + "\nWhy: needed\nHow: implement"}
    assert "Why: needed" in fmt.show_text(item)
    text = brief.brief_text(brief.build(item))
    assert "Why: needed" in text and "How: implement" in text
    assert len(text) <= brief.BRIEF_CAP
