"""Why/how enforcement uses a hermetic SQLite keeper, not a live service."""
from types import SimpleNamespace

import pytest

from keeper_util import World, defn
from plangraph import views
from plangraph.keeper import brief, fmt
from plangraph.keeper.brief import missing_why_how
from plangraph.keeper.cli_check import cmd_check
from plangraph.keeper.cli_verbs import cmd_define
from plangraph.keeper.mcp import handle
import json


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path, backend="sqlite")
    yield world
    world.close()


@pytest.mark.parametrize("description, missing", [
    ("How: implement", "Why:"), ("Why: useful", "How:"),
    ("Why:   \nHow: implement", "Why:"),
    ("why: useful\nHow: implement", "Why:"),
    ("prefix Why: useful\nHow: implement", "Why:"),
    ("Why:\nHow: implement", "Why:"),
])
def test_front_doors_refuse_missing_sections(w, tmp_path, capsys, description, missing):
    items = [defn("G", kind="goal", description=description)]
    result = handle({"id": 1, "method": "tools/call", "params": {
        "name": "create", "arguments": {"items": items}}}, object())["result"]
    assert result["isError"]
    assert "missing_why_how" in result["content"][0]["text"]
    assert missing in result["content"][0]["text"]
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items))
    assert cmd_define(SimpleNamespace(file=path, edit=False), object()) == 2
    error = capsys.readouterr().err
    assert "missing_why_how" in error and missing in error
    assert w.store.create_items(w.p("router"), items)["created"] == ["demo:G"]


@pytest.mark.parametrize("kind, description", [
    ("goal", "Why: useful\nHow: build"), ("decision", "short prose")])
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
    result = w.store.create_items(w.p("router"), [
        defn("D", kind="decision"),
        defn("G", ["D"], kind="goal", description="Why: useful\nHow: build")])
    assert result["created"] == ["demo:D", "demo:G"]


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
