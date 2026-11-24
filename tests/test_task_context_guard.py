"""TASK-CONTEXT-GUARD (D-3215): full task context on reads, stale claims refused.

A session must see, before it starts, that an item's work already sits on the
default branch, that a dependency was cancelled (and why), or that a later
decision changed its premise; ``claim`` refuses those cases with the facts
unless ``force_stale`` carries an audited reason.  The git side runs against a
real throw-away repository in ``tmp_path``; the decisions log is a temp file.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from keeper_util import World, defn, seed
from plangraph.keeper import auth, events as keeper_events, fmt, mcp
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.evidence_git import GitAdapter


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, name, message):
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    _commit(path, "base", "base")
    return path


@pytest.fixture
def decisions(tmp_path):
    path = tmp_path / "decisions-log.md"
    path.write_text("| ID | project | decision |\n|---|---|---|\n", encoding="utf-8")
    return path


@pytest.fixture
def w(tmp_path, repo, decisions):
    world = World(tmp_path)
    world.store.ctx = {"git": GitAdapter({"demo": {"path": str(repo), "ref": "main"}}),
                       "decisions": DecisionsAdapter(decisions)}
    try:
        yield world
    finally:
        world.close()


def _decide(path, did, text):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"| D-{did} | demo | {text} |\n")


def _merge_lane(repo, key, pr=7):
    """A lane branch with a Plan-Item commit, merged into main as ``Merge PR #pr``."""
    _git(repo, "checkout", "-q", "-b", "lane/x")
    sha = _commit(repo, "x", f"work\n\nPlan-Item: {key}")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", f"Merge PR #{pr} lane/x", "lane/x")
    return sha


# ------------------------------------------------------------------ show
def test_show_lists_the_merged_plan_item_commit_and_its_pr(w, repo):
    key, = seed(w, [("A", [])])
    sha = _merge_lane(repo, key)
    info = w.store.show(w.p("reader"), key)
    commits = info["work_exists"]["commits"]
    assert [c["sha"] for c in commits] == [sha]
    assert commits[0]["merge"]["pr"] == 7
    assert info["stale"] == f"already merged {sha[:8]}"
    text = fmt.show_text(info)
    assert f"merged_on_default {sha[:8]}" in text and "PR#7" in text
    assert "STALE already merged" in text


def test_a_longer_id_trailer_never_matches_the_shorter_key(w, repo):
    key, = seed(w, [("A", [])])
    _commit(repo, "fu", f"fix\n\nPlan-Item: {key}-FU")
    info = w.store.show(w.p("reader"), key)
    assert info["work_exists"]["commits"] == [] and info["stale"] is None


def test_the_bare_id_trailer_counts(w, repo):
    key, = seed(w, [("A", [])])
    sha = _commit(repo, "a", "work\n\nPlan-Item: A")
    assert [c["sha"] for c in w.store.show(w.p("reader"), key)
            ["work_exists"]["commits"]] == [sha]


def test_show_lists_an_unmerged_lane_branch(w, repo):
    key, = seed(w, [("LANE-ONE", [])])
    _git(repo, "checkout", "-q", "-b", "lane/lane-one")
    _commit(repo, "wip", "wip")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "lane/lane-one-merged")   # already on main: not open
    info = w.store.show(w.p("reader"), key)
    assert info["work_exists"]["lanes"] == ["lane/lane-one"]
    assert "open_lanes lane/lane-one" in fmt.show_text(info)


def test_show_carries_deps_dependents_reasons_decisions_and_last_activity(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("DEP", description="premise D-40"),
        defn("A", ["DEP"], description="built on D-41"),
        defn("NEXT", ["A"]), defn("GOAL", ["NEXT"], kind="goal")])
    w.store.cancel(w.p("router"), "demo:DEP", "superseded by the new design")
    _decide(decisions, 50, "demo:A stays as planned")
    info = w.store.show(w.p("reader"), "demo:A")
    assert info["dep_states"] == [{"key": "demo:DEP", "state": "cancelled",
                                   "reason": "superseded by the new design"}]
    assert info["dependents"] == [{"key": "demo:NEXT", "state": "open", "reason": None}]
    assert info["linked_decisions"] == ["D-41", "D-50"]
    assert info["newer_decisions"] == ["D-50"]
    last = info["last_activity"]
    assert last["actor"] == "router" and last["type"] == "item_created" and last["at"]
    text = fmt.show_text(info)
    assert "dep_reasons demo:DEP=cancelled(superseded by the new design)" in text
    assert "decisions D-41, D-50" in text and "last_activity " in text


def test_no_repo_means_unknown_not_refused(w):
    w.store.ctx = None
    key, = seed(w, [("A", [])])
    info = w.store.show(w.p("reader"), key)
    assert info["work_exists"] == {"commits": None, "lanes": None}
    assert "merged_on_default unknown" in fmt.show_text(info)
    assert w.store.claim(w.p("l1main"), key)["epoch"] == 1


def test_brief_carries_merged_and_stale(w, repo):
    key, = seed(w, [("A", [])])
    sha = _merge_lane(repo, key)
    brief = w.store.show(w.p("reader"), key, brief=True)
    assert brief["merged"] == f"{sha[:8]} PR#7"
    assert brief["stale"].startswith("already merged")


# ----------------------------------------------------------------- claim
def test_claim_refuses_an_item_already_merged(w, repo):
    key, = seed(w, [("A", [])])
    sha = _merge_lane(repo, key)
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), key)
    assert exc.value.code == "bad_state"
    assert "already merged" in exc.value.msg and sha[:8] in exc.value.msg
    assert "PR #7" in exc.value.msg and "force_stale" in exc.value.msg
    assert w.store._item(key)["state"] == "open"


def test_claim_checks_origin_again_even_with_a_recent_work_index(w, repo, tmp_path, monkeypatch):
    from time import time
    from types import SimpleNamespace
    from plangraph.keeper import context_sources

    # Freeze index age so the second claim happens inside any work-index TTL.
    monkeypatch.setattr(context_sources, "time", SimpleNamespace(monotonic=lambda: 10, time=time))
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(repo), str(checkout))
    w.store.ctx["git"] = GitAdapter({"demo": str(checkout)})
    key, = seed(w, [("A", [])])
    principal = w.p("l1main")
    first = w.store.claim(principal, key)
    w.store.release(principal, key, first["epoch"])

    sha = _merge_lane(repo, key)
    assert _git(checkout, "rev-parse", "origin/main") != _git(repo, "rev-parse", "main")
    with pytest.raises(Refused, match=f"already merged: Plan-Item commit {sha[:8]}"):
        w.store.claim(principal, key)
    assert w.store._item(key)["state"] == "open"


def test_force_stale_claims_and_is_audited_on_the_event(w, repo):
    key, = seed(w, [("A", [])])
    _merge_lane(repo, key)
    out = w.store.claim(w.p("l1main"), key, force_stale="slice 2 of 2 (Plan-Slice)")
    assert out["epoch"] == 1
    row = w.conn.execute("SELECT payload FROM events WHERE key=? AND type='claimed'",
                         (key,)).fetchone()
    payload = json.loads(row["payload"])
    assert payload["force_stale"]["reason"] == "slice 2 of 2 (Plan-Slice)"
    assert "already merged" in payload["force_stale"]["overrode"][0]


def test_force_stale_for_router_preflight_and_phase_worker_claim(w, repo):
    key, = seed(w, [("A", [])])
    sha = _merge_lane(repo, key)
    token = auth.add_principal(w.conn, "phase-worker", "phase-worker", ["demo"])
    worker = auth.authenticate(w.conn, token)
    router = w.p("router")
    reason = "Verify the remaining merged work"
    merged = f"already merged: Plan-Item commit {sha[:8]}"

    with pytest.raises(Refused, match=merged):
        w.store._prepare_claim(router, key, None, {})
    with pytest.raises(Refused, match=merged):
        w.store.claim(worker, key)
    assert w.store._item(key)["state"] == "open"

    _, override, _ = w.store._prepare_claim(router, key, reason, {})
    assert override == {"reason": reason, "overrode": [merged]}
    assert w.store.claim(worker, key, force_stale=reason)["epoch"] == 1
    row = w.conn.execute(
        "SELECT payload FROM events WHERE key=? AND type='claimed'", (key,)).fetchone()
    assert json.loads(row["payload"])["force_stale"] == override


def test_force_stale_must_be_a_string(w, repo):
    key, = seed(w, [("A", [])])
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), key, force_stale=True)
    assert exc.value.code == "validation"


def test_claim_refuses_when_a_newer_decision_names_the_item(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("GEN-LOWS", description="per D-10"), defn("GOAL", ["GEN-LOWS"], kind="goal")])
    # rows often drop the project prefix: a distinctive bare id counts
    _decide(decisions, 12, "GEN-LOWS: premise changed, start after P7")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:GEN-LOWS")
    assert "premise outdated: D-12" in exc.value.msg
    # re-confirmed: the item now cites the newer decision
    w.store.edit_definition(w.p("router"), "demo:GEN-LOWS",
                            {"description": "per D-10, re-confirmed by D-12"})
    assert w.store.claim(w.p("l1main"), "demo:GEN-LOWS")["epoch"] == 1


def test_a_short_bare_id_is_not_matched_in_decision_text(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("A", description="per D-10"), defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 12, "A: plan B wins")
    assert w.store.show(w.p("reader"), "demo:A")["newer_decisions"] == []


def test_an_older_decision_naming_the_item_does_not_refuse(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("A", description="per D-30"), defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 20, "demo:A first planned")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1


def test_the_decision_clock_orders_items_that_cite_no_decision(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("EARLIER", description="from D-20"), defn("A"),
        defn("GOAL", ["EARLIER", "A"], kind="goal")])
    _decide(decisions, 15, "demo:A named before")
    assert w.store.show(w.p("reader"), "demo:A")["newer_decisions"] == []
    _decide(decisions, 25, "demo:A premise moved")
    assert w.store.show(w.p("reader"), "demo:A")["newer_decisions"] == ["D-25"]


def test_claim_of_a_cancelled_item_says_why(w):
    key, = seed(w, [("A", [])])
    w.store.cancel(w.p("router"), key, "folded into B")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), key)
    assert exc.value.code == "locked"
    assert "is cancelled: folded into B" in exc.value.msg


def test_claim_with_a_cancelled_dep_names_the_dep_and_reason(w):
    keys = seed(w, [("DEP", []), ("A", ["DEP"])])
    w.store.cancel(w.p("router"), keys[0], "obsolete premise")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), keys[1])
    assert "demo:DEP=cancelled (obsolete premise)" in exc.value.msg


# ----------------------------------------------------------------- ready
def test_ready_flags_merged_items_and_sorts_them_last(w, repo):
    keys = seed(w, [("A", []), ("B", [])])
    w.store.set_priority(w.p("router"), keys[0], 5)
    _merge_lane(repo, keys[0])
    text = w.store.ready(w.p("reader"), project="demo")
    lines = text.splitlines()
    assert lines[0].startswith("demo:B |")
    assert lines[1] == "" and lines[2] == "Needs reconcile:"
    assert lines[3].startswith("  demo:A |") and "STALE: already merged" in lines[3]
    assert "NEEDS_RECONCILE: already merged" in lines[3]
    assert len(lines) == 4   # only the startable item has a top-level key line


def test_ready_flags_untouched_items_and_superseded_premises(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("A", description="per D-5"), defn("B"), defn("GOAL", ["A", "B"], kind="goal")])
    _decide(decisions, 9, "supersedes D-5 for demo")
    w.clock.advance(8 * 86400)
    rows = w.store._ready_items(project="demo")
    by_key = {r["key"]: r["context"]["stale"] for r in rows}
    assert by_key["demo:A"] == "untouched 8d; premise D-5 superseded by D-9"
    assert by_key["demo:B"] == "untouched 8d"


def test_ready_rows_without_context_render_unchanged():
    rows = [{"key": "demo:A", "title": "t", "estimate_h": 1}]
    assert fmt.ready_text(rows, server_items=True) == "demo:A | t | est 1h | unblocks 0 | prio 0"


# ------------------------------------------------------------------- mcp
def test_mcp_tools_say_read_show_first_and_carry_force_stale():
    tools = {tool["name"]: tool for tool in mcp.TOOLS}
    assert "ALWAYS read before work" in tools["show"]["description"]
    assert "never start from a title" in tools["show"]["description"]
    assert "force_stale" in tools["claim"]["inputSchema"]["properties"]
    assert "show before work" in tools["ready"]["description"]


def test_mcp_claim_passes_force_stale_through():
    seen = []

    class Client:
        def call(self, method, verb, args):
            seen.append((method, verb, dict(args)))
            return "ok", False

    mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "claim",
                           "arguments": {"key": "demo:A", "force_stale": "why"}}},
               Client())
    assert seen == [("POST", "claim", {"key": "demo:A", "force_stale": "why"})]


# ------------------------------------------- stale-premise watermark and reconfirm
def _newer(w, key):
    return w.store.show(w.p("router"), key)["newer_decisions"]


def _refuses(w, key):
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), key)
    assert "premise outdated" in exc.value.msg, exc.value.msg
    return exc.value.msg


_GOALS = iter(range(1000))


def _mk(w, *lanes):
    """Create *lanes* in one batch under a fresh goal (a batch without a goal is refused)."""
    goal = f"GOAL{next(_GOALS)}"
    w.store.create_items(w.p("router"), [*lanes, defn(goal, [d["id"] for d in lanes], kind="goal")])


def _created(w, key):
    return next(e["payload"] for e in keeper_events.read_events(w.conn)
                if e["key"] == key and e["type"] == "item_created")


_METADATA = [("session", "D-999999"), ("design_ref", "docs/D-999999.md"), ("similar", ["D-999999"]),
             ("category", "D-999999"), ("supersedes", "D-999999")]


@pytest.mark.parametrize("field,value", _METADATA)
def test_definition_metadata_does_not_advance_another_items_watermark(w, decisions, field, value):
    # C cites D-5800; A carries the metadata; B cites nothing and is defined after A, so B's
    # watermark is the clock at its own definition and no metadata id may be in it.
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("A", **{field: value}))
    _mk(w, defn("B"))
    assert "D-999999" not in _created(w, "demo:A").get("note", "")
    assert w.store._decision_clock()["marks"][-1] == 5800
    _decide(decisions, 5900, "demo:B now also covers the web; supersedes the old scope")
    assert "D-5900" in _refuses(w, "demo:B")
    assert _newer(w, "demo:B") == ["D-5900"]
    # the item itself: its own metadata is never a citation either
    _decide(decisions, 5901, "demo:A scope changed")
    assert "D-5901" in _refuses(w, "demo:A")


def test_a_session_is_metadata_on_the_creation_event(w):
    _mk(w, defn("A", session="D-999999"))
    assert _created(w, "demo:A")["session"] == "D-999999"
    assert "D-999999" not in (w.store._item("demo:A")["note"] or "")


def test_an_imported_session_stays_out_of_the_premise(w, decisions, tmp_path):
    from plangraph.keeper.importer import load_dir
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "p.yaml").write_text(
        "items:\n"
        "- {project: demo, id: C, title: c, description: per D-5800, owner: tester, track: T0, estimate_h: 1, kind: lane}\n"
        "- {project: demo, id: A, title: a, session: D-999999, owner: tester, track: T0, estimate_h: 1, kind: lane}\n"
        "- {project: demo, id: Z, title: z, owner: tester, track: T0, estimate_h: 1, kind: lane}\n"
        "- {project: demo, id: GOAL, title: g, kind: goal, depends_on: [A, Z, C], owner: tester, track: T0, estimate_h: 1}\n",
        encoding="utf-8")
    items = load_dir(plan)
    assert [i["session"] for i in items if i["id"] == "A"] == ["D-999999"]
    assert all("D-999999" not in (i.get("note") or "") for i in items)
    w.store.import_items(w.p("router"), items)
    assert _created(w, "demo:A")["session"] == "D-999999"
    assert w.store._decision_clock()["marks"][-1] == 5800
    _decide(decisions, 5900, "demo:Z scope changed")          # Z is imported after C: its watermark is 5800
    assert "D-5900" in _refuses(w, "demo:Z")


@pytest.mark.parametrize("bad", [7, "", "a\nb", "a\rb", "a\n", "a\r", "a\u2028", "a\x85b", "x" * 201, "pass" + "word: " + "svc" + "acct/" + "Zq9xk2LmPw"])
def test_a_session_must_be_a_one_line_credential_free_string(w, bad):
    with pytest.raises(Refused) as exc:
        _mk(w, defn("A", session=bad))
    assert "session" in exc.value.msg
    _mk(w, defn("OK", session="sess-1"))       # the same batch shape passes with a good one


@pytest.mark.parametrize("field", ["title", "description", "done_criteria", "note", "source"])
def test_every_premise_field_of_a_definition_moves_the_clock(w, decisions, field):
    value = "per D-5800"
    _mk(w, defn("X", **{field: value}))
    _mk(w, defn("B"))
    assert _created(w, "demo:X")[field] == value
    assert not {"title", "done_criteria", "note", "source"} & set(_created(w, "demo:B"))   # no cite, no key
    assert w.store._decision_clock()["marks"][-1] == 5800
    _decide(decisions, 5900, "demo:B scope changed")
    assert _newer(w, "demo:B") == ["D-5900"]            # the event-derived fallback


@pytest.mark.parametrize("field", ["title", "description", "done_criteria", "note"])
def test_an_edit_of_a_premise_field_moves_the_clock(w, decisions, field):
    _mk(w, defn("X"))
    assert w.store._decision_clock()["marks"][-1] == -1
    edit = {field: "per D-5900"}
    if field == "done_criteria":      # the criteria ledger wants a reason; it is not premise text
        edit.update(criteria_reason="clarify", criteria_evidence="ledger entry")
    w.store.edit_definition(w.p("router"), "demo:X", edit)
    assert w.store._decision_clock()["marks"][-1] == 5900


def test_an_edited_premise_moves_the_watermark_through_its_fields(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    assert _newer(w, "demo:A") == ["D-5900"]
    w.store.edit_definition(w.p("router"), "demo:A", {"description": "per D-5900"})
    assert _newer(w, "demo:A") == []


def test_the_items_own_source_decision_claims_without_force(w, decisions):
    # a definition that cites D-5800 comes first, so A's fallback watermark is 5800 and
    # only its own source citation can keep D-5862 from being newer
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("A", source="D-5862"), defn("B", description="per D-5800", source="D-5850"))
    _decide(decisions, 5862, "demo:A discussion of the rollout")
    assert _newer(w, "demo:A") == []
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1
    # another row naming the item, newer than the source, still refuses
    _decide(decisions, 5870, "demo:B scope changed")
    assert "D-5870" in _refuses(w, "demo:B")


def test_a_row_that_is_not_the_source_and_not_reconfirmed_keeps_the_gate(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5858, "OPERATOR order: demo:A ordered first, GO")
    assert "D-5858" in _refuses(w, "demo:A")


def test_reconfirm_clears_exactly_up_to_the_decision_and_is_audited(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    _decide(decisions, 5910, "demo:A scope changed again")
    before = w.store.show(w.p("router"), "demo:A")["description"]
    assert w.store.reconfirm(w.p("router"), "demo:A", "D-5900") == {
        "key": "demo:A", "reconfirmed": "D-5900"}
    assert w.store.show(w.p("router"), "demo:A")["description"] == before
    assert _newer(w, "demo:A") == ["D-5910"]               # exactly up to D-5900
    marks = [e for e in keeper_events.read_events(w.conn)
             if e["key"] == "demo:A" and e["type"] == "premise_reconfirmed"]
    assert len(marks) == 1 and marks[0]["actor"] == "router"
    assert marks[0]["payload"]["decision"] == "D-5900"
    w.store.reconfirm(w.p("router"), "demo:A", "D-5910")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1
    w.store.release(w.p("l1main"), "demo:A", 1)
    _decide(decisions, 5950, "demo:A changes once more")   # a later decision refuses again
    assert "D-5950" in _refuses(w, "demo:A")
    assert _newer(w, "demo:A") == ["D-5950"]


def test_the_floor_is_per_item(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("B", description="per D-5800"),
                                         defn("GOAL", ["A", "B"], kind="goal")])
    _decide(decisions, 5900, "demo:A and demo:B scope changed")
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")
    assert _newer(w, "demo:A") == [] and _newer(w, "demo:B") == ["D-5900"]


def test_reconfirm_also_clears_a_retired_premise(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 9, "supersedes D-5 for demo")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:A")
    assert "premise D-5 superseded by D-9" in exc.value.msg
    w.store.reconfirm(w.p("router"), "demo:A", "D-9")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1


def test_a_system_lease_reads_the_same_floor(w, decisions):
    w.store.create_items(w.p("router"), [defn("PREM", description="per D-5"),
                                         defn("GOAL", ["PREM"], kind="goal")])
    _decide(decisions, 9, "supersedes D-5 for demo")
    with pytest.raises(Refused) as exc:
        w.store.worker_claim_lease(w.p("system"), "demo:PREM")
    assert "premise D-5 superseded by D-9" in exc.value.msg
    w.store.reconfirm(w.p("router"), "demo:PREM", "D-9")
    assert w.store.worker_claim_lease(w.p("system"), "demo:PREM")["epoch"] == 1


def test_a_system_lease_refuses_a_newer_decision_until_reconfirmed_and_again_after_a_later_one(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    with pytest.raises(Refused) as exc:                      # refused before the reconfirm
        w.store.worker_claim_lease(w.p("system"), "demo:A")
    assert "premise outdated: D-5900" in exc.value.msg
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")
    assert w.store.worker_claim_lease(w.p("system"), "demo:A")["epoch"] == 1
    w.store.release(w.p("router"), "demo:A", 1)
    _decide(decisions, 5950, "demo:A changes again")
    with pytest.raises(Refused) as exc:                      # and again after a later decision
        w.store.worker_claim_lease(w.p("system"), "demo:A")
    assert "premise outdated: D-5950" in exc.value.msg


def test_ready_reads_the_same_floor(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    groups = w.store._ready_groups(project="demo")
    assert [i["key"] for i in groups["items"]] != ["demo:A"] or groups["needs_reconcile"]
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")
    groups = w.store._ready_groups(project="demo")
    assert "demo:A" in [i["key"] for i in groups["items"]]
    assert "demo:A" not in [i["key"] for i in groups["needs_reconcile"]]


def test_reconfirm_refuses_an_unknown_decision_a_bad_id_and_a_stranger(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("router"), "demo:A", "D-77777")
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("l1other"), "demo:A", "D-5900")
    with pytest.raises(Refused):                    # the router records it, not the claimant's L1
        w.store.reconfirm(w.p("l1main"), "demo:A", "D-5900")
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("router"), "demo:A", "not-a-decision")
    assert _newer(w, "demo:A") == ["D-5900"]


def test_the_cli_and_the_api_carry_the_verb():
    from plangraph.keeper import api_routes, cli
    assert "reconfirm" in api_routes.WRITE_VERBS and "reconfirm" in api_routes.ROLE_CHECKED_VERBS
    args = cli.build_parser().parse_args(["reconfirm", "demo:A", "D-5900"])
    assert (args.key, args.decision) == ("demo:A", "D-5900")


def test_a_citation_embedded_in_a_free_text_source_is_a_premise_citation(w, decisions):
    _mk(w, defn("A", source="Implementation per D-5"))
    _decide(decisions, 5, "an old plan")
    _decide(decisions, 9, "supersedes D-5 for demo")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:A")
    assert "premise D-5 superseded by D-9" in exc.value.msg
    # the same text raises the watermark of the item: a decision it cites is not newer
    _mk(w, defn("B", source="Implementation per D-5862"))
    _decide(decisions, 5862, "demo:B discussion of the rollout")
    assert _newer(w, "demo:B") == []


def test_unconfirmed_order_and_same_session_rows_refuse_claim(w, decisions):
    # the old lane let a terse GO row and a row of the item's own session claim without a
    # reconfirm; nothing is read from the prose of a row, so both refuse until reconfirmed
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("A", session="sess-77"), defn("B"))
    _decide(decisions, 5858, "GO demo:A")
    _decide(decisions, 5859, "(sess-77) demo:B ordered first, GO")
    assert "D-5858" in _refuses(w, "demo:A")
    assert "D-5859" in _refuses(w, "demo:B")
    w.store.reconfirm(w.p("router"), "demo:A", "D-5858")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1


def test_a_metadata_only_edit_does_not_move_the_fallback_anchor(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("U"))
    _mk(w, defn("E", description="per D-6000"))
    w.store.edit_definition(w.p("router"), "demo:U", {"host": "elsewhere"})   # no premise text
    _decide(decisions, 5950, "demo:U scope changed")
    assert _newer(w, "demo:U") == ["D-5950"]           # still read against the premise at its creation
    w.store.edit_definition(w.p("router"), "demo:U", {"note": "reworded"})    # a real premise change
    assert _newer(w, "demo:U") == []                   # now anchored after D-6000


def test_a_premise_edit_invalidates_the_floor_of_the_earlier_read(w, decisions):
    _mk(w, defn("A", description="per D-5800"))
    _decide(decisions, 5, "an old plan")
    _decide(decisions, 9, "supersedes D-5 for demo")
    _decide(decisions, 5900, "demo:A scope changed")
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1
    w.store.release(w.p("router"), "demo:A", 1)
    # the router read a premise that did not cite D-5; a later edit introduces the retired id
    w.store.edit_definition(w.p("router"), "demo:A", {"description": "per D-5800 and D-5"})
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:A")
    assert "premise D-5 superseded by D-9" in exc.value.msg
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")      # a new read clears it again
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 2


def test_system_and_phase_worker_leases_read_the_decision_log_fresh_and_strict(w, decisions):
    from plangraph.keeper import auth
    token = auth.add_principal(w.conn, "phase-runner", "phase-worker", ["demo"])
    runner = auth.authenticate(w.conn, token)
    _mk(w, defn("A", description="per D-5800"))
    _decide(decisions, 5900, "demo:Z scope changed")           # same length as the row below
    assert _newer(w, "demo:A") == []                           # caches the book of this file version
    stat = decisions.stat()
    decisions.write_text(decisions.read_text(encoding="utf-8").replace("demo:Z", "demo:A"), encoding="utf-8")
    os.utime(decisions, ns=(stat.st_atime_ns, stat.st_mtime_ns))     # same size, same mtime
    for who in (w.p("system"), runner):
        with pytest.raises(Refused) as exc:
            w.store.worker_claim_lease(who, "demo:A")
        assert "premise outdated: D-5900" in exc.value.msg
    with pytest.raises(Refused):                               # the ordinary claim agrees
        w.store.claim(w.p("l1main"), "demo:A")
    decisions.unlink()                                         # an unreadable log refuses both doors
    for who in (w.p("system"), runner):
        with pytest.raises(Refused) as exc:
            w.store.worker_claim_lease(who, "demo:A")
        assert "cannot read decisions file" in exc.value.msg


@pytest.mark.parametrize("field", ["category", "similar", "supersedes"])
def test_a_multiline_metadata_value_stays_out_of_the_note_and_the_clock(w, decisions, field):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("A", **{field: "first line\nsecond line cites D-999999\nthird line"}))
    _mk(w, defn("B"))
    assert w.store._item("demo:A")["note"] is None                 # never folded into the note
    assert w.store._decision_clock()["marks"][-1] == 5800
    _decide(decisions, 5900, "demo:B scope changed")
    assert "D-5900" in _refuses(w, "demo:B")


def test_a_note_that_begins_with_a_metadata_name_keeps_its_citations(w, decisions):
    _mk(w, defn("A", note="category: see D-5 for the history"))
    _decide(decisions, 5, "an old plan")
    _decide(decisions, 9, "supersedes D-5 for demo")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:A")
    assert "premise D-5 superseded by D-9" in exc.value.msg


def test_saving_an_identical_note_does_not_move_the_anchor(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("U", note="plain note"))
    _mk(w, defn("E", description="per D-6000"))
    w.store.edit_definition(w.p("router"), "demo:U", {"note": "plain note"})    # the same text
    _decide(decisions, 5950, "demo:U scope changed")
    assert _newer(w, "demo:U") == ["D-5950"]                  # the anchor is still U's creation


def test_saving_an_identical_note_keeps_the_floor_and_a_changed_one_voids_it(w, decisions):
    _mk(w, defn("V", description="per D-5800", note="plain note"))
    _decide(decisions, 5950, "demo:V scope changed")
    w.store.reconfirm(w.p("router"), "demo:V", "D-5950")
    w.store.edit_definition(w.p("router"), "demo:V", {"note": "plain note"})
    assert _newer(w, "demo:V") == []                          # a re-save does not reopen the refusal
    w.store.edit_definition(w.p("router"), "demo:V", {"note": "another note"})
    assert _newer(w, "demo:V") == ["D-5950"]                  # a change of the premise does


def test_reverting_a_premise_edit_does_not_revive_the_old_floor(w, decisions):
    _mk(w, defn("V", description="per D-5800", note="first note"))
    _decide(decisions, 5950, "demo:V scope changed")
    w.store.reconfirm(w.p("router"), "demo:V", "D-5950")
    assert _newer(w, "demo:V") == []
    w.store.edit_definition(w.p("router"), "demo:V", {"note": "second note"})
    assert _newer(w, "demo:V") == ["D-5950"]                  # the edit voids the floor
    w.store.edit_definition(w.p("router"), "demo:V", {"note": "first note"})   # revert
    assert _newer(w, "demo:V") == ["D-5950"]                  # and the revert does not bring it back
    with pytest.raises(Refused):
        w.store.claim(w.p("l1main"), "demo:V")


def test_resaving_a_legacy_note_does_not_publish_its_folded_metadata(w, decisions):
    # a note an older Keeper folded metadata into: its D-999999 line is not a citation of this
    # premise, and an edit that re-lists the note unchanged must not make it one
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("L", note="plain note"), defn("B"))
    legacy = "plain note\nsession: D-999999"
    w.conn.execute("UPDATE items SET note=? WHERE key='demo:L'", (legacy,))   # as an old Keeper left it
    w.store.edit_definition(w.p("router"), "demo:L", {"note": legacy})
    assert w.store._decision_clock()["marks"][-1] == 5800
    _mk(w, defn("Z"))
    _decide(decisions, 5900, "demo:Z scope changed")
    assert "D-5900" in _refuses(w, "demo:Z")


def test_an_export_keeps_the_metadata_and_a_reimport_gets_it_back(w, tmp_path):
    from plangraph.keeper.importer import export_yaml, load_dir
    _mk(w, defn("A", session="sess-9", category="infra", similar=["demo:B"], supersedes="demo:OLD"))
    out = tmp_path / "export"
    export_yaml(w.store, out)
    items = {i["id"]: i for i in load_dir(out)}
    assert [items["A"][n] for n in ("session", "category", "similar", "supersedes")] == [
        "sess-9", "infra", ["demo:B"], "demo:OLD"]
    assert "session" not in (items["A"].get("note") or "")


def test_an_imported_credential_label_in_the_note_covers_the_metadata_value(w, tmp_path):
    from plangraph.keeper.importer import load_dir
    plan = tmp_path / "plan"
    plan.mkdir()
    secret = "svc" + "acct/" + "Zq9xk2LmPw"
    (plan / "p.yaml").write_text(
        "items:\n"
        f"- {{project: demo, id: A, title: a, note: 'the password is', session: '{secret}', owner: tester,"
        " track: T0, estimate_h: 1, kind: lane}\n", encoding="utf-8")
    with pytest.raises(Refused) as exc:
        load_dir(plan)
    assert "credential" in exc.value.msg and secret not in exc.value.msg
