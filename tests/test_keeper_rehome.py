"""MODEL-HOST: ``rehome`` moves a non-DONE item to another project, with aliases."""
from types import SimpleNamespace

import pytest

from keeper_util import World, defn, seed, to_done, to_claimed
from plangraph import views
from plangraph.keeper import auth, fmt, mcp
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.events import verify_integrity

OLD, NEW = "workstation:A", "plangraph:A"


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", ["A"])], project="workstation",
         review_tier="standard")
    world.store.edit_definition(world.p("router"), OLD,
                                {"description": "move me", "host": "box"})
    for name, role, family in (("wsl1", "l1", "teamA"), ("wsrev", "reviewer", "teamC")):
        token = auth.add_principal(world.conn, name, role,
                                   ["workstation", "plangraph"], family=family)
        world.tokens[name] = token
        world.principals[name] = auth.authenticate(world.conn, token)
    yield world
    world.close()


@pytest.fixture
def doors(w, tmp_path):
    from door_util import Doors
    d = Doors(w, tmp_path)
    yield d
    d.close()


def _rehome(w, key=OLD, project="plangraph", host="workstation", who="router"):
    return w.store.rehome(w.p(who), key, project, host)


def _edges(w):
    return {(r["src"], r["dst"]) for r in w.conn.execute("SELECT src,dst FROM edges")}


def test_rehome_creates_new_key_and_cancels_old(w):
    result = _rehome(w)
    assert result["old"] == OLD and result["new"] == NEW
    new, old = w.store._item(NEW), w.store._item(OLD)
    assert (new["state"], new["host"], new["description"], new["title"]) == (
        "open", "workstation", "move me", "title A")
    assert old["state"] == "cancelled"
    reason = w.conn.execute(
        "SELECT payload FROM events WHERE key=? AND type='cancelled'",
        (OLD,)).fetchone()[0]
    assert "rehomed to plangraph:A" in reason


def test_rehome_repoints_dependents_and_copies_own_deps(w):
    w.store.apply_ops(w.p("router"), [
        {"op": "create", "item": defn("C", ["A"], project="workstation")},
        {"op": "edit", "key": "workstation:GOAL",
         "fields": {"depends_on": ["A", "B", "C"]}}])
    _rehome(w, "workstation:B", "plangraph")  # B depends on A, GOAL depends on B
    edges = _edges(w)
    assert ("plangraph:B", OLD) in edges            # own deps copied
    assert ("workstation:GOAL", "plangraph:B") in edges
    assert ("workstation:GOAL", "workstation:B") not in edges
    _rehome(w, OLD, "plangraph")                    # now A moves
    edges = _edges(w)
    assert ("plangraph:B", NEW) in edges and ("workstation:C", NEW) in edges
    assert not [e for e in edges if e[1] == OLD
                and w.store._item(e[0])["state"] != "cancelled"]
    assert views.validate(w.store._all_items()) == [] or all(
        p["code"] == "depends_on_cancelled" for p in views.validate(
            w.store._all_items()))
    assert verify_integrity(w.conn) == []


def test_alias_recorded_and_listed(w):
    _rehome(w)
    rows = w.store.aliases(w.p("reader"))
    assert [(r["old"], r["new"]) for r in rows] == [(OLD, NEW)]
    assert w.store.resolve_key(OLD) == NEW and w.store.resolve_key(NEW) == NEW
    assert "workstation:A -> plangraph:A" in fmt.aliases_text(rows)


def test_alias_chain_resolves_to_the_end(w):
    _rehome(w)
    _rehome(w, NEW, "other")
    assert w.store.resolve_key(OLD) == "other:A"


def test_done_item_is_refused_and_stays_put(w):
    to_done(w, OLD, who="wsl1", reviewer="wsrev")
    before = w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with pytest.raises(Refused) as exc:
        _rehome(w)
    assert exc.value.code in ("locked", "bad_state")
    assert w.store._item(NEW) is None and w.store.resolve_key(OLD) == OLD
    assert w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_claimed_item_is_refused(w):
    to_claimed(w, OLD, who="wsl1")
    with pytest.raises(Refused) as exc:
        _rehome(w)
    assert exc.value.code == "bad_state"


def test_router_only(w):
    for who in ("l1main", "workerA", "reader"):
        with pytest.raises(Refused) as exc:
            _rehome(w, who=who)
        assert exc.value.code == "forbidden"


@pytest.mark.parametrize("project", ["workstation", "", "a:b"])
def test_bad_target_project_refused(w, project):
    with pytest.raises(Refused) as exc:
        _rehome(w, project=project)
    assert exc.value.code == "validation"


def test_existing_target_key_conflicts(w):
    w.store.create_items(w.p("router"), [defn("A", project="plangraph"),
                                         defn("G", ["A"], project="plangraph",
                                              kind="goal")])
    with pytest.raises(Refused) as exc:
        _rehome(w)
    assert exc.value.code == "conflict"


def test_bad_host_refused_and_nothing_written(w):
    with pytest.raises(Refused):
        _rehome(w, host="Bad Host")
    assert w.store._item(NEW) is None and w.store._item(OLD)["state"] == "open"


def test_unreachable_after_move_refused_and_rolled_back(w):
    """A locked (cancelled) dependent cannot be re-pointed, so the moved item
    would lose its only path to a goal: the whole move is refused."""
    router = w.p("router")
    w.store.apply_ops(router, [
        {"op": "create", "item": defn("X", project="workstation")},
        {"op": "create", "item": defn("C", ["X"], project="workstation")},
        {"op": "edit", "key": "workstation:GOAL",
         "fields": {"depends_on": ["A", "B", "C"]}}])
    w.store.cancel(router, "workstation:C", "obsolete")
    events_before = w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    edges_before = _edges(w)
    with pytest.raises(Refused) as exc:
        _rehome(w, "workstation:X")
    assert exc.value.code == "validation" and "unreachable_goal" in exc.value.msg
    assert w.store._item("plangraph:X") is None
    assert w.store._item("workstation:X")["state"] == "open"
    assert w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
    assert _edges(w) == edges_before and w.store.aliases(router) == []
    assert verify_integrity(w.conn) == []


def test_failure_mid_move_rolls_everything_back(w, monkeypatch):
    real = w.store._append
    calls = []

    def boom(principal, key, type, payload, now):
        calls.append(type)
        if type == "cancelled":
            raise RuntimeError("disk full")
        return real(principal, key, type, payload, now)

    monkeypatch.setattr(w.store, "_append", boom)
    events_before = w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    edges_before = _edges(w)
    with pytest.raises(Exception):
        _rehome(w)
    monkeypatch.undo()
    assert "item_created" in calls
    assert w.store._item(NEW) is None and w.store._item(OLD)["state"] == "open"
    assert w.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
    assert _edges(w) == edges_before
    assert w.store.aliases(w.p("reader")) == []
    assert verify_integrity(w.conn) == []


# ---------------------------------------------------- alias resolution doors

def test_api_resolves_an_old_key(w, doors):
    _rehome(w)
    status, info = doors.http("reader", "GET", f"/v1/show?key={OLD}&fmt=json")
    assert status == 200 and info["key"] == NEW and info["host"] == "workstation"
    status, res = doors.http("router", "POST", "/v1/set_priority",
                             {"key": OLD, "n": 7})
    assert status == 200 and w.store._item(NEW)["priority"] == 7
    status, rows = doors.http("reader", "GET", "/v1/aliases?fmt=json")
    assert status == 200 and rows[0]["old"] == OLD


def test_api_rehome_verb_is_router_only(w, doors):
    body = {"key": OLD, "new_project": "plangraph", "host": "workstation"}
    status, _ = doors.http("l1main", "POST", "/v1/rehome", body)
    assert status == 403
    status, res = doors.http("router", "POST", "/v1/rehome", body)
    assert status == 200 and res["new"] == NEW


def test_cli_rehome_aliases_and_show(w, doors):
    code, out, _ = doors.pg("router", "rehome", OLD, "plangraph",
                            "--host", "workstation")
    assert code == 0 and NEW in out
    code, out, _ = doors.pg("reader", "show", OLD)
    assert code == 0 and out.startswith(NEW) and "host workstation" in out
    code, out, _ = doors.pg("reader", "aliases")
    assert code == 0 and f"{OLD} -> {NEW}" in out


def test_mcp_show_resolves_an_old_key(w, doors):
    _rehome(w)
    token_file = doors.tmp / "mcp.token"
    token_file.write_text(w.tok("reader"), encoding="utf-8")
    keeper = mcp.HttpKeeper(doors.url, str(token_file), timeout=5)
    reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "show",
                                   "arguments": {"key": OLD}}}, keeper)
    assert reply["result"]["isError"] is False
    assert NEW in reply["result"]["content"][0]["text"]


def test_apply_ops_resolves_keys_and_has_a_rehome_op(w):
    router = w.p("router")
    w.store.apply_ops(router, [
        {"op": "rehome", "key": OLD, "new_project": "plangraph",
         "host": "workstation"},
        {"op": "edit", "key": OLD, "fields": {"title": "renamed in batch"}},
        {"op": "priority", "key": OLD, "priority": 3}])
    assert w.store._item(NEW)["title"] == "renamed in batch"
    assert w.store._item(NEW)["priority"] == 3 and verify_integrity(w.conn) == []
    w.store.apply_ops(router, [{"op": "edit", "key": OLD,
                                "fields": {"note": "later batch via the alias"}}])
    assert w.store._item(NEW)["note"] == "later batch via the alias"


def test_apply_ops_rehome_rolls_back_with_the_batch(w):
    router = w.p("router")
    with pytest.raises(Refused):
        w.store.apply_ops(router, [
            {"op": "rehome", "key": OLD, "new_project": "plangraph"},
            {"op": "edit", "key": "nope:X", "fields": {"title": "x"}}])
    assert w.store._item(NEW) is None and w.store.aliases(router) == []
    assert verify_integrity(w.conn) == []


def test_depends_on_an_old_key_resolves_in_create_and_edit(w):
    router = w.p("router")
    _rehome(w)
    w.store.create_items(router, [
        defn("N", [OLD], project="workstation"),
        defn("N2", ["N"], project="workstation", kind="goal")])
    assert NEW in w.store._item("workstation:N")["depends_on"]
    w.store.edit_definition(router, "workstation:N", {"depends_on": [OLD]})
    assert w.store._item("workstation:N")["depends_on"] == [NEW]


def test_waives_token_naming_the_old_key_counts_for_the_new_key(w, tmp_path):
    path = tmp_path / "DECISIONS.md"
    path.write_text("| ID | Note | Waiver scope |\n|---|---|---|\n"
                    f"| D-217 | waiver | Waives: {OLD} |\n", encoding="utf-8")
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(path))
    w.store.edit_definition(w.p("router"), OLD, {"review_tier": "high"})
    _rehome(w)
    to_claimed(w, NEW, who="wsl1")
    assert w.store.waive_second_family(w.p("router"), NEW, "D-217")


def test_closes_token_naming_the_old_key_counts_for_the_new_key(w, tmp_path):
    path = tmp_path / "DECISIONS.md"
    path.write_text("| ID | Note | Decision |\n|---|---|---|\n"
                    f"| D-5 | close | Closes: {OLD} |\n", encoding="utf-8")
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(path))
    _rehome(w)
    w.store.set_legacy_state(w.p("router"), NEW, "claimed")
    w.store.add_evidence_router(w.p("router"), NEW, "decision", {"id": "D-5"})
    item = w.store._item(NEW)
    _rows, named = w.store._decision_evidence(NEW, item, w.store.now())
    assert NEW in named and OLD not in named


def test_fold_and_table_agree_and_a_forged_alias_is_caught(w):
    import sqlite3
    _rehome(w)
    assert verify_integrity(w.conn) == []
    raw = sqlite3.connect(str(w.path))
    raw.execute("INSERT INTO aliases(old_key,new_key) VALUES('workstation:B','other:B')")
    raw.commit()
    raw.close()
    assert [(p["code"], p["key"], p["expected"], p["got"])
            for p in verify_integrity(w.conn)] == [
        ("alias_mismatch", "workstation:B", None, "other:B")]


def test_snapshot_lists_aliases_for_the_offline_cli(w):
    _rehome(w)
    assert w.store.snapshot(w.p("reader"))["aliases"] == {OLD: NEW}
    from plangraph.keeper.cli_local import local_show
    items = w.store.snapshot(w.p("reader"))["items"]
    assert local_show(items, OLD, {OLD: NEW})["key"] == NEW
