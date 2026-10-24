"""PG-BRIEF (D-708 A): short task briefs by key, capped at 1600 chars.

Pattern: tests/test_keeper_reads.py (World fixture) for the store door,
tests/test_keeper_description.py (Doors) for HTTP/CLI, tests/test_mcp.py
(``mcp.handle``) for the MCP door.

Mutation killed by ``test_brief_cap_and_truncation_marker``: deleting the
``ensure_cap`` call in ``brief.build`` (or raising ``BRIEF_CAP``) lets the
full 2000-char description through, so the text exceeds 1600 chars and the
marker assertion fails.
"""
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from keeper_util import World, defn, seed, to_done
from plangraph.keeper import api, brief as brief_mod, config, fmt, mcp

BRIEF_KEYS = {"key", "title", "state", "kind", "owner", "review_tier",
              "evidence_kinds", "priority", "wait", "depends_on",
              "description", "done_criteria", "note"}
FORBIDDEN_KEYS = {"events", "evidence", "reviews", "aliases", "epoch",
                  "unblocks", "lease_until"}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", ["A"])])
    world.store.edit_definition(world.p("router"), "demo:A",
                                {"description": "does the thing",
                                 "note": "first line\nlatest line"})
    yield world
    world.close()


def test_brief_fields_present_and_nothing_else(w):
    info = w.store.show(w.p("reader"), "demo:A", brief=True)
    assert info["key"] == "demo:A" and info["title"] == "title A"
    assert info["state"] == "open" and info["kind"] == "lane"
    assert info["owner"] == "tester" and info["review_tier"] == "standard"
    assert info["evidence_kinds"] == ["any"] and info["priority"] == 0
    assert info["depends_on"] == [] and "wait" not in info
    assert info["description"] == "does the thing"
    assert info["done_criteria"] == "criteria"
    assert info["note"] == "latest line"  # only the latest note line
    assert set(info) <= BRIEF_KEYS and not (set(info) & FORBIDDEN_KEYS)


def test_brief_wait_included_when_set(w):
    item = dict(w.store._item("demo:A"))
    item["wait"] = "answer"
    info = brief_mod.build(item, [])
    assert info["wait"] == "answer"
    assert "wait answer" in brief_mod.brief_text(info)


def test_brief_deps_carry_states(w):
    before = w.store.show(w.p("reader"), "demo:B", brief=True)
    assert before["depends_on"] == [{"key": "demo:A", "state": "open"}]
    to_done(w, "demo:A")
    after = w.store.show(w.p("reader"), "demo:B", brief=True)
    assert after["depends_on"] == [{"key": "demo:A", "state": "done"}]


def test_brief_cap_and_truncation_marker(w):
    long_desc = "d" * 2000
    w.store.edit_definition(w.p("router"), "demo:B",
                            {"description": long_desc})
    info = w.store.show(w.p("reader"), "demo:B", brief=True)
    text = brief_mod.brief_text(info)
    assert len(text) <= brief_mod.BRIEF_CAP
    assert "…[+" in info["description"] and info["description"] != long_desc
    assert info["key"] == "demo:B" and info["title"] == "title B"
    assert info["state"] == "open"
    assert info["depends_on"] == [{"key": "demo:A", "state": "open"}]
    assert "demo:A=open" in text  # deps are never truncated


def test_brief_truncates_done_criteria_after_description(w):
    w.store.edit_definition(w.p("router"), "demo:B",
                            {"done_criteria": "c" * 600,
                             "note": "n\n" + "x" * 1200})
    info = w.store.show(w.p("reader"), "demo:B", brief=True)
    text = brief_mod.brief_text(info)
    assert len(text) <= brief_mod.BRIEF_CAP
    # description (absent here) is spent first: done_criteria takes the cut
    assert info["done_criteria"] != "c" * 600 and "…[+" in info["done_criteria"]
    assert info["note"] == "x" * 1200  # the note survives this time
    assert info["key"] == "demo:B" and info["state"] == "open"


def test_brief_truncates_note_only_as_a_last_resort(w):
    w.store.edit_definition(w.p("router"), "demo:B",
                            {"description": "short",
                             "note": "n\n" + "x" * 1900})
    info = w.store.show(w.p("reader"), "demo:B", brief=True)
    text = brief_mod.brief_text(info)
    assert len(text) <= brief_mod.BRIEF_CAP
    assert info["description"] == "short"  # never touched before the note
    assert info["done_criteria"] == "criteria"
    assert "…[+" in (info.get("note") or "")


def test_create_without_description_warns_but_is_not_refused(w):
    result = w.store.create_items(w.p("router"), [
        defn("X", kind="lane"), defn("G2", ["X"], kind="goal")])
    assert result["created"] == ["demo:X", "demo:G2"]
    assert result["warnings"] == ["description missing: brief will be empty"]
    assert w.store._item("demo:X") is not None


def test_create_with_description_has_no_warnings(w):
    result = w.store.create_items(w.p("router"), [
        defn("X", description="has one"),
        defn("G2", ["X"], kind="goal", description="goal")])
    assert result["created"] == ["demo:X", "demo:G2"]
    assert "warnings" not in result


# ------------------------------------------------------------- HTTP and CLI

@pytest.fixture
def doors(w, tmp_path):
    from door_util import Doors
    doors = Doors(w, tmp_path)
    yield doors
    doors.close()


def test_http_show_brief_text_and_json(doors):
    status, body = doors.http("reader", "GET", "/v1/show?key=demo:A&brief=1")
    assert status == 200 and isinstance(body, str)
    assert "demo:A | title A" in body and "desc does the thing" in body
    assert "note latest line" in body and "reviews" not in body
    assert len(body) <= brief_mod.BRIEF_CAP
    status, info = doors.http(
        "reader", "GET", "/v1/show?key=demo:A&brief=1&fmt=json")
    assert status == 200 and info["description"] == "does the thing"
    assert set(info) <= BRIEF_KEYS


def test_http_show_without_brief_is_unchanged(doors):
    status, info = doors.http("reader", "GET", "/v1/show?key=demo:A&fmt=json")
    assert status == 200 and "evidence" in info and "reviews" in info


def test_http_create_reports_the_warning(doors):
    from keeper_util import defn as _defn
    status, body = doors.http("router", "POST", "/v1/create_items",
                              {"items": [_defn("X"),
                                         _defn("G2", ["X"], kind="goal")]})
    assert status == 200
    assert body["warnings"] == ["description missing: brief will be empty"]


def test_cli_show_brief_text_and_json(doors):
    code, out, _ = doors.pg("reader", "show", "demo:A", "--brief")
    assert code == 0
    assert "desc does the thing" in out and "reviews" not in out
    assert len(out.strip()) <= brief_mod.BRIEF_CAP
    code, out, _ = doors.pg("reader", "show", "demo:A", "--brief",
                            json_out=True)
    assert code == 0 and json.loads(out)["key"] == "demo:A"


# ------------------------------------------------------------------- MCP

@pytest.fixture
def live(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    world.store.edit_definition(world.p("router"), "demo:A",
                                {"description": "over mcp"})
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps({
        "db": str(world.path), "snapshot": str(tmp_path / "snap.json"),
        "bind": "127.0.0.1:0", "reap_interval": 3600}), encoding="utf-8")
    os.chmod(path, 0o600)
    server = api.make_server(world.store, config.load_config(path), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield world, f"http://127.0.0.1:{server.server_address[1]}", tmp_path
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    world.close()


def _client(live, name):
    world, base, tmp = live
    token_path = tmp / f"{name}.token"
    token_path.write_text(world.tok(name), encoding="utf-8")
    return mcp.HttpKeeper(base, str(token_path), timeout=10)


def test_mcp_show_brief_true(live):
    reply = mcp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "show",
                    "arguments": {"key": "demo:A", "brief": True}}},
        _client(live, "l1main"))
    assert reply["result"]["isError"] is False
    text = reply["result"]["content"][0]["text"]
    assert "desc over mcp" in text and "reviews" not in text
    assert len(text) <= brief_mod.BRIEF_CAP


def test_mcp_show_without_brief_is_unchanged(live):
    reply = mcp.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "show", "arguments": {"key": "demo:A"}}},
        _client(live, "l1main"))
    assert reply["result"]["isError"] is False
    assert "reviews" in reply["result"]["content"][0]["text"]


def test_mcp_create_reports_the_warning(live):
    reply = mcp.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "create", "arguments": {"items": [
             defn("W1"), defn("W1G", ["W1"], kind="goal")]}}},
        _client(live, "router"))
    assert reply["result"]["isError"] is False
    assert "description missing: brief will be empty" in \
        reply["result"]["content"][0]["text"]


# ------------------------------------------------------------------ stats

def test_brief_stats_reports_median_and_p90(w, tmp_path, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import brief_stats
    cfg_path = tmp_path / "keeper.json"
    cfg_path.write_text(json.dumps({
        "db": str(w.path),
        "snapshot": str(tmp_path / "plan.snapshot.json")}),
        encoding="utf-8")
    os.chmod(cfg_path, 0o600)
    assert brief_stats.main(["--keeper-config", str(cfg_path)]) == 0
    out = capsys.readouterr().out
    assert "open items: 3" in out
    assert "median brief:" in out and "chars" in out and "tokens" in out
    assert "p90 brief:" in out


def test_fmt_brief_text_is_the_capped_text(w):
    info = w.store.show(w.p("reader"), "demo:A", brief=True)
    assert fmt.brief_text(info) == brief_mod.brief_text(info)
    assert len(fmt.brief_text(info)) <= brief_mod.BRIEF_CAP
