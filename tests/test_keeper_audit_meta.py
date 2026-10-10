"""PG-AUDIT-META (D-1012/D-1013): the read verb ``audit_meta``.

Mutations killed: a missing-metadata count that includes a DONE/in-review item;
the ``estimate_src: default`` marker counted as an estimate; a gate or operator
kind audited for ``estimate_h``/``done_criteria``; an l1 shown another project's
keys or counts; a stale claim measured off the wall clock instead of the
injected ``now``; a group keyed by ``owner`` when ``claimed_by`` is set; the
suggested actions swapped; a skeleton whose ``source_sha256`` does not match its
sibling note file; ``--json`` and text disagreeing; a worker reaching the read.
"""
from __future__ import annotations

import hashlib
import json
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import apply_batch  # noqa: E402
from door_util import Doors  # noqa: E402
from keeper_util import (  # noqa: E402
    World, defn, to_claimed, to_done, to_in_review)
from plangraph.keeper import api_routes  # noqa: E402
from plangraph.keeper import audit_meta as am  # noqa: E402
from plangraph.keeper import mcp  # noqa: E402
from plangraph.keeper.errors import Refused  # noqa: E402

DAY = 86400
DEMO_STUBS = ["demo:BLANK", "demo:DEFEST", "demo:NODC", "demo:NODESC",
              "demo:NOEST"]


def _clean(pid, deps=(), **kw):
    """A definition with every audited field present."""
    item = {"description": "what " + pid, "estimate_src": "plan"}
    item.update(kw)
    return defn(pid, list(deps), **item)


_counter = [0]


def _create(world, specs, project="demo", who="router"):
    """Create ``(id, kwargs)`` items plus a fresh goal that keeps the graph valid."""
    _counter[0] += 1
    goal = f"G{_counter[0]}"
    items = [_clean(pid, project=project, **kw) for pid, kw in specs]
    items.append(_clean(goal, [pid for pid, _ in specs], project=project,
                        kind="goal"))
    world.store.create_items(world.p(who), items)
    return [f"{project}:{pid}" for pid, _ in specs]


@pytest.fixture
def w(tmp_path):
    """demo carries one finding per audited field, plus three stale claims."""
    world = World(tmp_path)
    _create(world, [
        ("CLEAN", {}),
        ("NODESC", {"description": None}),
        ("BLANK", {"description": "   ", "owner": " "}),
        ("NOEST", {"estimate_h": None}),
        ("DEFEST", {"estimate_h": 3, "estimate_src": "default"}),
        ("NODC", {"done_criteria": None}),
        ("S1", {}), ("S2", {}),
    ])
    # excluded kinds: one gate and each operator kind, no estimate, no criteria
    _create(world, [("X1", {"kind": "gate", "estimate_h": None,
                             "done_criteria": None})])
    _create(world, [("OQ", {"kind": "question", "estimate_h": None,
                            "done_criteria": None}),
                    ("OD", {"kind": "decision", "estimate_h": None,
                            "done_criteria": None}),
                    ("OO", {"kind": "operator_task", "estimate_h": None,
                            "done_criteria": None})])
    # everything missing, but DONE: never reported
    _create(world, [("GHOST", {"description": None, "estimate_h": None,
                               "estimate_src": None, "done_criteria": None,
                               "owner": None})])
    to_done(world, "demo:GHOST", who="l1main")
    # a clean item in another project: the router sees it, an l1 of demo must not
    _create(world, [("OK", {})], project="other")
    # --- the stale claims all happen at the clock's opening instant
    to_claimed(world, "demo:S1", who="l1main")
    epoch = to_claimed(world, "demo:S2", who="l1main")
    world.store.add_evidence(world.p("l1main"), "demo:S2", epoch, "commit+ci",
                             {"commit": "abc1234"})              # merged proof
    world.store.import_items(world.p("router"), [
        _clean("L1", state="claimed", owner="legacy-ghost"),
        _clean("ZZ", ["L1"], kind="goal")])
    world.clock.advance(4 * DAY)
    yield world
    world.close()


# ------------------------------------------------------------------- helpers

def _data(world, who="router", project=None, stale_days=None):
    kw = {} if stale_days is None else {"stale_days": stale_days}
    return world.store.audit_meta(world.p(who), project=project, **kw)


def _project(data, name):
    return next(p for p in data["projects"] if p["project"] == name)


def _missing(data, name, field):
    entry = next(m for m in _project(data, name)["missing"] if m["field"] == field)
    return entry["count"], entry["keys"]


def _field_lines(text):
    """``field -> (count, keys)`` of the *first* project block that lists it."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in am.MISSING_FIELDS:
            keys = [k for k in "".join(parts[2:]).split(",") if k and k != "-"]
            out.setdefault(parts[0], (int(parts[1]), keys))
    return out


def _stale(data, name="demo"):
    return [item for group in _project(data, name)["stale_groups"]
            for item in group["items"]]


# ----------------------------------------------------------- missing metadata

def test_counts_and_sorted_keys_per_field(w):
    data = _data(w)
    assert _missing(data, "demo", "description") == (2, ["demo:BLANK", "demo:NODESC"])
    assert _missing(data, "demo", "estimate_h") == (2, ["demo:DEFEST", "demo:NOEST"])
    assert _missing(data, "demo", "done_criteria") == (1, ["demo:NODC"])
    assert _missing(data, "demo", "owner") == (1, ["demo:BLANK"])
    assert [m["field"] for m in _project(data, "demo")["missing"]] == \
        list(am.MISSING_FIELDS)
    assert _missing(data, "other", "description") == (0, [])
    assert _missing(data, "demo", "estimate_h")[1] == \
        sorted(_missing(data, "demo", "estimate_h")[1])


def test_a_blank_value_counts_as_missing(w):
    data = _data(w)
    assert "demo:BLANK" in _missing(data, "demo", "description")[1]
    assert "demo:BLANK" in _missing(data, "demo", "owner")[1]


def test_the_estimate_default_marker_is_not_an_estimate(w):
    keys = _missing(_data(w), "demo", "estimate_h")[1]
    assert "demo:DEFEST" in keys        # estimate_src: default, nothing stated
    assert "demo:NOEST" in keys         # estimate_h NULL
    assert "demo:CLEAN" not in keys     # estimate_src: plan


def test_missing_items_counts_each_item_once(w):
    demo = _project(_data(w), "demo")
    assert demo["missing_items"] == 5
    assert sum(m["count"] for m in demo["missing"]) == 6


def test_gates_and_operator_kinds_are_excluded(w):
    data = _data(w)
    for field in ("estimate_h", "done_criteria"):
        keys = _missing(data, "demo", field)[1]
        for key in ("demo:X1", "demo:OQ", "demo:OD", "demo:OO"):
            assert key not in keys, (field, key)
    for key in ("demo:X1", "demo:OQ", "demo:OD", "demo:OO"):
        for field in ("description", "owner"):
            assert key not in _missing(data, "demo", field)[1]


def test_a_done_item_is_never_reported(w):
    data = _data(w)
    for field in am.MISSING_FIELDS:
        assert "demo:GHOST" not in _missing(data, "demo", field)[1], field


def test_an_in_review_item_is_not_audited(w):
    assert "demo:NODESC" in _missing(_data(w), "demo", "description")[1]
    to_in_review(w, "demo:NODESC", who="l1main")
    assert "demo:NODESC" not in _missing(_data(w), "demo", "description")[1]


def test_a_cancelled_item_is_not_reported(w):
    w.store.cancel(w.p("router"), "demo:NODESC", "drop")
    assert "demo:NODESC" not in _missing(_data(w), "demo", "description")[1]


# ------------------------------------------------------------------ staleness

def test_stale_uses_the_injected_clock_and_groups_by_claimant(w):
    data = _data(w)
    assert data["stale_days"] == 3
    groups = _project(data, "demo")["stale_groups"]
    assert [g["holder"] for g in groups] == ["l1main", "legacy-ghost"]
    assert [g["count"] for g in groups] == [2, 1]
    assert [i["key"] for i in groups[0]["items"]] == ["demo:S1", "demo:S2"]
    assert _project(data, "demo")["stale_items"] == 3
    assert all(i["age_days"] >= 4.0 for i in _stale(data))
    assert all(i["last_ts"] for i in _stale(data))


def test_the_group_falls_back_to_owner_and_releases_an_empty_claim(w):
    """``claimed_by`` empty -> the holder is ``owner`` and the action is release."""
    item = {"key": "p:A", "project": "p", "state": "claimed", "kind": "lane",
            "title": "A", "claimed_by": "", "owner": "owna",
            "description": "d", "estimate_h": 1, "estimate_src": "plan",
            "done_criteria": "c"}
    data = am.build([item], {"p:A": "1970-01-01T00:00:00Z"}, set(), {"owna"},
                    now=4 * DAY, now_iso="1970-01-05T00:00:00Z", stale_days=3)
    group = data["projects"][0]["stale_groups"][0]
    assert group["holder"] == "owna"
    assert group["items"][0]["action"] == "release"


def test_action_close_by_d_row_when_merged_commit_evidence_exists(w):
    stale = {i["key"]: i["action"] for i in _stale(_data(w))}
    assert stale["demo:S2"] == "close-by-D-row"
    assert stale["demo:S1"] == "renew"


def test_action_release_for_an_unknown_claimant(w):
    group = next(g for g in _project(_data(w), "demo")["stale_groups"]
                 if any(i["key"] == "demo:L1" for i in g["items"]))
    assert group["holder"] == "legacy-ghost"
    assert next(i for i in group["items"]
                if i["key"] == "demo:L1")["action"] == "release"


def test_action_release_past_double_the_window(w):
    stale = {i["key"]: i["action"] for i in _stale(_data(w))}
    assert stale["demo:S1"] == "renew"              # 4 days, N = 3
    w.clock.advance(3 * DAY)                        # 7 days > 2N
    stale = {i["key"]: i["action"] for i in _stale(_data(w))}
    assert stale["demo:S1"] == "release"


def test_stale_days_moves_the_threshold(w):
    assert sorted(i["key"] for i in _stale(_data(w))) == \
        ["demo:L1", "demo:S1", "demo:S2"]
    assert _stale(_data(w, stale_days=10)) == []
    assert _stale(_data(w, stale_days=2)) == _stale(_data(w, stale_days=3))
    _create(w, [("S4", {})])
    to_claimed(w, "demo:S4", who="l1main")
    w.clock.advance(2 * DAY)
    assert [i["key"] for i in _stale(_data(w, stale_days=1))
            if i["key"] == "demo:S4"] == ["demo:S4"]
    assert [i["key"] for i in _stale(_data(w, stale_days=3))
            if i["key"] == "demo:S4"] == []


def test_an_item_with_no_events_is_never_stale(w):
    assert "demo:CLEAN" not in [i["key"] for i in _stale(_data(w))]


@pytest.mark.parametrize("bad", [0, -1, "junk", "1.5", "99999", True])
def test_stale_days_is_validated(w, bad):
    with pytest.raises(Refused) as exc:
        w.store.audit_meta(w.p("router"), stale_days=bad)
    assert exc.value.code == "validation"


# ---------------------------------------------------------------------- scope

def test_router_sees_every_project(w):
    data = _data(w)
    assert [p["project"] for p in data["projects"]] == ["demo", "other"]
    assert data["totals"]["projects"] == 2


def test_l1_sees_only_its_own_projects(w):
    data = _data(w, who="l1main")
    assert [p["project"] for p in data["projects"]] == ["demo"]
    assert "other" not in json.dumps(data)
    assert data["totals"]["projects"] == 1
    assert [p["project"] for p in _data(w, who="l1other")["projects"]] == ["other"]


def test_l1_naming_another_project_is_refused(w):
    with pytest.raises(Refused) as exc:
        _data(w, who="l1main", project="other")
    assert exc.value.code == "forbidden"
    assert "other:" not in exc.value.msg              # no key leaks either way


def test_project_filter_keeps_only_that_project(w):
    data = _data(w, project="other")
    assert [p["project"] for p in data["projects"]] == ["other"]
    assert _project(data, "other")["missing_items"] == 0
    assert _data(w, who="l1main", project="demo")["totals"]["projects"] == 1


@pytest.mark.parametrize("who", ["workerA", "reviewer", "reader", "system"])
def test_only_the_router_and_l1_roles_may_read(w, who):
    with pytest.raises(Refused) as exc:
        _data(w, who=who)
    assert exc.value.code == "forbidden"


def test_a_run_token_may_not_reach_the_read():
    with pytest.raises(Refused) as exc:
        api_routes.run_gate(SimpleNamespace(token_id=7), "audit-meta")
    assert exc.value.code == "forbidden"


def test_the_read_is_audited_like_every_verb(w):
    _data(w)
    row = w.conn.execute("SELECT action,ok,actor,role FROM audit_log"
                         " WHERE action='audit_meta' ORDER BY id DESC LIMIT 1"
                         ).fetchone()
    assert tuple(row) == ("audit_meta", 1, "router", "router")
    with pytest.raises(Refused):
        _data(w, who="l1main", project="other")
    bad = w.conn.execute("SELECT ok,code FROM audit_log WHERE action='audit_meta'"
                         " AND ok=0 ORDER BY id DESC LIMIT 1").fetchone()
    assert (bad["ok"], bad["code"]) == (0, "forbidden")


# ------------------------------------------------------------ text and json

def test_text_output_is_stable_and_sorted(w):
    text = am.render(_data(w))
    assert text == am.render(_data(w))
    lines = text.splitlines()
    assert lines[0] == (f"audit-meta now {w.store._iso(w.clock())} |"
                        " stale-days 3 | projects demo, other")
    fields = _field_lines(text)
    assert fields["description"] == (2, ["demo:BLANK", "demo:NODESC"])
    assert fields["done_criteria"] == (1, ["demo:NODC"])
    assert "  stale l1main (2)" in text
    assert "-> close-by-D-row" in text and "-> renew" in text
    assert lines[-1].startswith("totals: projects 2 |")


def test_text_and_json_carry_the_same_data(w):
    data = _data(w)
    assert am.render(json.loads(json.dumps(data))) == am.render(data)


def test_an_empty_scope_renders_without_sections(tmp_path):
    world = World(tmp_path)
    try:
        data = world.store.audit_meta(world.p("router"))
        assert data["projects"] == [] and data["totals"]["audited"] == 0
        lines = am.render(data).splitlines()
        assert lines[0].endswith("projects -")
        assert lines[-1].startswith("totals: projects 0 |")
    finally:
        world.close()


# -------------------------------------------------------------------- skeleton

@pytest.fixture
def skeleton(w, tmp_path):
    out = tmp_path / "skel"
    written = am.write_skeleton(_data(w), out)
    return out, written


def test_skeleton_writes_one_yaml_and_one_note_per_project(skeleton):
    out, written = skeleton
    assert sorted(p.name for p in out.iterdir()) == \
        ["demo.BATCH.md", "demo.BATCH.yaml"]        # "other" has no findings
    assert sorted(written) == [out / "demo.BATCH.md", out / "demo.BATCH.yaml"]


def test_skeleton_one_edit_op_per_item_with_every_missing_field(skeleton):
    out, _ = skeleton
    data = apply_batch.load_batch(str(out / "demo.BATCH.yaml"))
    assert data["batch"] == "audit-meta-demo" and data["principal"] == "router"
    assert data["source"] == "demo.BATCH.md"
    assert data["source_sha256"] == hashlib.sha256(
        (out / "demo.BATCH.md").read_bytes()).hexdigest()
    by_key = {}
    for op in data["ops"]:
        assert op["op"] == "edit" and set(op) == {"op", "key", "fields"}
        assert all(value == "" for value in op["fields"].values())
        by_key.setdefault(op["key"], []).extend(sorted(op["fields"]))
    assert sorted(by_key) == DEMO_STUBS
    assert by_key["demo:BLANK"] == ["description", "owner"]
    assert len([op for op in data["ops"] if op["key"] == "demo:BLANK"]) == 1
    text = (out / "demo.BATCH.yaml").read_text(encoding="utf-8")
    assert "# demo:BLANK | title BLANK | state open" in text     # title + fields
    assert "done_criteria=criteria" in text


def test_skeleton_passes_the_batch_applier_and_dry_run(skeleton, tmp_path):
    out, _ = skeleton
    data = apply_batch.load_batch(str(out / "demo.BATCH.yaml"))
    for op in data["ops"]:
        apply_batch.validate_op(op)
    assert apply_batch.resolve_source(str(out / "demo.BATCH.yaml"),
                                      data["source"]) == str(out / "demo.BATCH.md")
    token = tmp_path / "tok"
    token.write_text("x" * 32, encoding="utf-8")
    code = apply_batch.main([str(out / "demo.BATCH.yaml"), "--url",
                             "http://127.0.0.1:1", "--token-file", str(token),
                             "--dry-run"], out=StringIO(), err=StringIO())
    assert code == 0


def test_skeleton_creates_the_directory_and_skips_silent_projects(w, tmp_path):
    out = tmp_path / "deep" / "nested"
    assert len(am.write_skeleton(_data(w), out)) == 2 and out.is_dir()
    quiet = _data(w, project="other")
    assert am.write_skeleton(quiet, tmp_path / "quiet") == []


def test_the_note_file_names_every_item_and_the_recovery_path(skeleton):
    out, _ = skeleton
    note = (out / "demo.BATCH.md").read_text(encoding="utf-8")
    for key in DEMO_STUBS:
        assert key in note
    assert "apply_batch.py" in note and "source_sha256" in note
    assert "claimed" in note                       # the edit-only-while-open caveat


# ---------------------------------------------------------------------- doors

@pytest.fixture
def doors(w, tmp_path):
    d = Doors(w, tmp_path)
    yield d
    d.close()


def test_the_verb_is_a_read_and_defaults_to_text():
    assert "audit-meta" in api_routes.READ_VERBS
    assert "audit-meta" not in api_routes.WRITE_VERBS
    assert "audit-meta" in api_routes.TEXT_DEFAULT


def test_http_door_text_and_json(w, doors):
    status, text = doors.http("router", "GET", "/v1/audit-meta")
    assert status == 200 and text == am.render(_data(w))    # HTTP text: no framing
    status, data = doors.http("router", "GET",
                              "/v1/audit-meta?fmt=json&stale_days=5")
    assert status == 200 and data["stale_days"] == 5
    status, data = doors.http("l1main", "GET", "/v1/audit-meta?fmt=json")
    assert status == 200 and [p["project"] for p in data["projects"]] == ["demo"]
    status, _ = doors.http("l1main", "GET", "/v1/audit-meta?project=other")
    assert status == 403
    status, _ = doors.http("workerA", "GET", "/v1/audit-meta")
    assert status == 403
    status, body = doors.http("router", "GET", "/v1/audit-meta?stale_days=junk")
    assert status == 400 and body["error"] == "validation"
    status, _ = doors.http("router", "POST", "/v1/audit-meta", {})
    assert status in (404, 405)


def test_cli_door_text_json_and_skeleton(w, doors, tmp_path):
    code, out, err = doors.pg("router", "audit-meta")
    assert code == 0, err
    assert out == am.render(_data(w)) + "\n"
    code, out, _ = doors.pg("router", "audit-meta", "--project", "demo",
                            "--stale-days", "5", json_out=True)
    data = json.loads(out)
    assert code == 0 and data["stale_days"] == 5
    assert [p["project"] for p in data["projects"]] == ["demo"]
    out_dir = tmp_path / "cli-skel"
    code, _, err = doors.pg("router", "audit-meta", "--skeleton", str(out_dir))
    assert code == 0, err
    assert (out_dir / "demo.BATCH.yaml").is_file() and "demo.BATCH.yaml" in err
    code, out, _ = doors.pg("router", "audit-meta", "--skeleton", str(out_dir),
                            json_out=True)
    assert code == 0 and json.loads(out)["stale_days"] == 3
    code, _, err = doors.pg("l1main", "audit-meta", "--project", "other")
    assert code == 2 and "forbidden" in err
    code, _, err = doors.pg("router", "audit-meta", "--stale-days", "0")
    assert code == 2 and "validation" in err


def test_mcp_lists_and_calls_the_tool(doors):
    tools = {t["name"]: t for t in mcp.TOOLS}
    assert "audit_meta" in tools
    assert mcp._ROUTES["audit_meta"] == ("GET", "audit-meta")
    token = doors.tmp / "router.mcp.token"
    token.write_text(doors.world.tok("router"), encoding="utf-8")
    client = mcp.HttpKeeper(doors.url, str(token))
    reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "audit_meta",
                                   "arguments": {"project": "demo"}}}, client)
    assert reply["result"]["isError"] is False
    assert "demo:BLANK" in reply["result"]["content"][0]["text"]
