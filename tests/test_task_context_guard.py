"""TASK-CONTEXT-GUARD (D-3215): full task context on reads, stale claims refused.

A session must see, before it starts, that an item's work already sits on the
default branch, that a dependency was cancelled (and why), or that a later
decision changed its premise; ``claim`` refuses those cases with the facts
unless ``force_stale`` carries an audited reason.  The git side runs against a
real throw-away repository in ``tmp_path``; the decisions log is a temp file.
"""
from __future__ import annotations

import json
import subprocess
import time

import pytest

from keeper_util import World, defn, seed
from plangraph.keeper import auth, fmt, mcp
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



# ---- PREMISE-EVENT-WATERMARK: the shared clock reads recorded premise values only ----

_GOALS = iter(range(1000))
_AMEND = {"criteria_reason": "scope moved", "criteria_evidence": "see the decision"}


def _mk(w, *lanes):
    """Create *lanes* in one batch under a fresh goal (a batch without a goal is refused)."""
    goal = f"GOAL{next(_GOALS)}"
    w.store.create_items(w.p("router"), [*lanes, defn(goal, [d["id"] for d in lanes], kind="goal")])


def _newer(w, key):
    return w.store.show(w.p("router"), key)["newer_decisions"]


def _refuses(w, key, who=None):
    with pytest.raises(Refused) as exc:
        w.store.claim(who or w.p("l1main"), key)
    assert "premise outdated" in exc.value.msg, exc.value.msg
    return exc.value.msg


def _clock_top(w):
    return w.store._decision_clock()["marks"][-1]


def test_definition_event_metadata_cannot_hide_a_newer_decision(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("A", design_ref="docs/D-999999.md"))
    w.store.edit_definition(w.p("router"), "demo:A", {"design_ref": "docs/D-999998.md"})
    _mk(w, defn("B"))
    assert _clock_top(w) == 5800
    _decide(decisions, 5900, "demo:B now also covers the web")
    assert "D-5900" in _refuses(w, "demo:B")


def test_definition_event_uses_only_changed_after_premise_fields(w, decisions):
    _mk(w, defn("A", done_criteria="per D-999999"))
    w.store.edit_definition(w.p("router"), "demo:A",
                            {"done_criteria": "per D-5800", **_AMEND})
    _mk(w, defn("R", done_criteria="per D-999999"))
    w.store.edit_definition(w.p("router"), "demo:R", {"done_criteria": "per D-999999"})  # resave
    _mk(w, defn("B"))
    assert _clock_top(w) == 5800
    _decide(decisions, 5700, "demo:B first planned")
    assert _newer(w, "demo:B") == []
    _decide(decisions, 5900, "demo:B scope moved")
    assert _newer(w, "demo:B") == ["D-5900"]
    assert "D-5900" in _refuses(w, "demo:B")


def test_a_ledger_only_criteria_event_reads_the_amendment_row(w, decisions):
    # the shape 6a588d61f wrote: the event names the field only, the ledger holds the values
    _mk(w, defn("A"))
    with w.store._write():
        seq, _ = w.store._append(w.p("router"), "demo:A", "item_defined",
                                 {"fields": ["done_criteria"]}, time.time())
        w.conn.execute(
            "INSERT INTO criteria_amendments(seq,key,original_text,new_text,reason,evidence,"
            "authority,ts) VALUES(?,?,?,?,?,?,?,?)",
            (seq, "demo:A", "per D-999999", "per D-5800", "r", "e", "router", "2026-01-01T00:00:00Z"))
    _mk(w, defn("B"))
    assert _clock_top(w) == 5800
    _decide(decisions, 5700, "demo:B first planned")
    _decide(decisions, 5900, "demo:B scope moved")
    msg = _refuses(w, "demo:B")
    assert "D-5900" in msg and "D-5700" not in msg


def test_metadata_only_edit_does_not_move_the_fallback_anchor(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("U"))
    _mk(w, defn("E", description="per D-6000"))
    w.store.edit_definition(w.p("router"), "demo:U", {"design_ref": "docs/x.md"})
    _decide(decisions, 5950, "demo:U scope changed")
    assert "D-5950" in _refuses(w, "demo:U")
    w.store.edit_definition(w.p("router"), "demo:U", {"description": "reworded"})
    assert _newer(w, "demo:U") == []
    assert w.store.claim(w.p("l1main"), "demo:U")["epoch"] == 1


def test_rehome_keeps_the_original_fallback_anchor(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("U"))
    _mk(w, defn("E", description="per D-6000"))
    w.store.rehome(w.p("router"), "demo:U", "other", "box")
    assert _clock_top(w) == 6000                      # the rehome advanced nothing
    _decide(decisions, 5900, "other:U scope changed")
    assert "D-5900" in _refuses(w, "other:U", w.p("l1other"))


def test_migrated_description_does_not_publish_carried_citations(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("U"))
    w.store.edit_definition(w.p("router"), "demo:U", {"note": "description: per D-999999"})
    w.store.migrate_descriptions(w.p("router"))
    assert _clock_top(w) == 5800
    w.store.rehome(w.p("router"), "demo:U", "other", "box")
    w.store.edit_definition(w.p("router"), "other:U", {"description": "per D-999999 and D-6000"})
    _mk(w, defn("L"))
    assert _clock_top(w) == 6000
    _decide(decisions, 5900, "demo:L first planned")
    _decide(decisions, 6100, "demo:L scope moved")
    msg = _refuses(w, "demo:L")
    assert "D-6100" in msg and "D-5900" not in msg


def test_a_note_only_event_supplies_no_shared_clock_evidence(w, decisions):
    # a partial-merge note carries the whole note, with no field boundaries: its D-ids stay out
    _mk(w, defn("N"))
    with w.store._write():
        w.store._append(w.p("router"), "demo:N", "item_defined", {"note": "per D-5800"}, time.time())
    _mk(w, defn("L"))
    assert _clock_top(w) == -1
    _decide(decisions, 5900, "demo:L scope moved")
    assert w.store.claim(w.p("l1main"), "demo:L")["epoch"] == 1       # unknown watermark never refuses


def test_a_cleared_description_moves_the_fallback_anchor(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("U", description="plain words"))
    _mk(w, defn("E", description="per D-6000"))
    _decide(decisions, 5900, "demo:U scope changed")
    assert "D-5900" in _refuses(w, "demo:U")
    w.store.edit_definition(w.p("router"), "demo:U", {"description": None})
    assert _newer(w, "demo:U") == []
    assert w.store.claim(w.p("l1main"), "demo:U")["epoch"] == 1


def test_a_changed_audit_value_publishes_the_citations_it_keeps(w, decisions):
    _mk(w, defn("A", done_criteria="per D-5800"))
    w.store.edit_definition(w.p("router"), "demo:A",
                            {"done_criteria": "per D-5800 and the new scope", **_AMEND})
    _mk(w, defn("B"))
    assert _clock_top(w) == 5800
    _decide(decisions, 5900, "demo:B scope moved")
    assert "D-5900" in _refuses(w, "demo:B")
