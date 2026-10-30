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

import pytest

from keeper_util import World, defn, seed
from plangraph.keeper import fmt, mcp
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
    assert lines[1].startswith("demo:A |") and "STALE: already merged" in lines[1]
    assert len(lines) == 2   # one line per item, no footer


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
