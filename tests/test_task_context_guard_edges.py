"""TASK-CONTEXT-GUARD (D-3215) edge cases: unknown facts, bounds, override audit.

Shares the real throw-away repository and decisions-log fixtures of
``test_task_context_guard``.
"""
from __future__ import annotations

import json

import pytest

from keeper_util import defn, seed
from plangraph.keeper import fmt
from plangraph.keeper.errors import Refused
from test_task_context_guard import (  # noqa: F401  (fixtures)
    _commit, _decide, _git, _merge_lane, decisions, repo, w)

class _Proc:
    def __init__(self, code, out=""):
        self.returncode, self.stdout = code, out


@pytest.fixture
def merge_history(repo):
    """Nested batch parts and a plain PR refreshed from main before merging."""
    direct = _commit(repo, "direct", "direct first-parent work")
    _git(repo, "branch", "batch")
    parts = []
    for pr in (895, 896):
        branch = f"part-{pr}"
        _git(repo, "checkout", "-q", "-b", branch, "main")
        parts.append(_commit(repo, branch, f"part {pr}"))
        _git(repo, "checkout", "-q", "batch")
        _git(repo, "merge", "-q", "--no-ff", "-m", f"part (#{pr})", branch)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "batch (#903)", "batch")
    batch = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "plain")
    plain = _commit(repo, "plain", "plain PR work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "advance")
    _commit(repo, "advance", "advance main")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "unrelated PR (#900)", "advance")
    _git(repo, "checkout", "-q", "plain")
    _git(repo, "merge", "-q", "--no-ff", "-m", "refresh from main", "main")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "plain (#899)", "plain")
    plain_merge = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "unmerged")
    unmerged = _commit(repo, "unmerged", "not on main")
    _git(repo, "checkout", "-q", "main")
    return {
        "part-895": (parts[0], batch, 903),
        "part-896": (parts[1], batch, 903),
        "plain": (plain, plain_merge, 899),
        "direct": (direct, batch, 903),
        "unmerged": (unmerged, None, None),
    }


@pytest.mark.parametrize("case", ["part-895", "part-896", "plain", "direct", "unmerged"])
def test_merge_of_finds_the_oldest_containing_first_parent_merge(w, repo, merge_history, case):
    from plangraph.keeper.context_sources import _merge_of
    sha, expected, pr = merge_history[case]
    merge = _merge_of(w.store.ctx["git"], str(repo), "main", sha)
    if expected is None:
        assert merge is None
    else:
        assert merge == {"sha": expected, "subject": "plain (#899)" if pr == 899
                         else "batch (#903)", "pr": pr}


def test_merge_of_ignores_older_unrelated_merges_in_the_scan_window(
        w, repo, merge_history, monkeypatch):
    from plangraph.keeper import context_sources
    monkeypatch.setattr(context_sources, "_MERGE_SCAN", 2)
    sha, expected, pr = merge_history["plain"]
    merge = context_sources._merge_of(w.store.ctx["git"], str(repo), "main", sha)
    assert merge == {"sha": expected, "subject": "plain (#899)", "pr": pr}


@pytest.mark.parametrize("failure", ["ancestry", "first-parent", "full-window"])
def test_merge_of_is_unknown_when_a_walk_fails_or_is_truncated(
        w, repo, merge_history, monkeypatch, failure):
    from plangraph.keeper import context_sources
    adapter = w.store.ctx["git"]
    real = adapter._run

    def run(repo_path, args, **kw):
        flag = "--ancestry-path" if failure == "ancestry" else "--first-parent"
        if failure != "full-window" and args[0] == "rev-list" and flag in args:
            return _Proc(128)
        return real(repo_path, args, **kw)

    monkeypatch.setattr(adapter, "_run", run)
    if failure == "full-window":
        monkeypatch.setattr(context_sources, "_MERGE_SCAN", 2)
    assert context_sources._merge_of(
        adapter, str(repo), "main", merge_history["part-895"][0]) is None


def test_a_git_failure_is_unknown_and_never_refuses(w, repo, monkeypatch):
    key, = seed(w, [("A", [])])
    _merge_lane(repo, key)
    monkeypatch.setattr(w.store.ctx["git"], "_run", lambda *a, **k: _Proc(128))
    info = w.store.show(w.p("reader"), key)
    assert info["work_exists"] == {"commits": None, "lanes": None}
    assert w.store.claim(w.p("l1main"), key)["epoch"] == 1


def test_open_lanes_is_unknown_when_the_ancestry_check_fails(w, repo, monkeypatch):
    key, = seed(w, [("LANE-TWO", [])])
    _git(repo, "branch", "lane/lane-two")
    adapter = w.store.ctx["git"]
    real = adapter._run

    def run(repo_path, args, **kw):
        return _Proc(128) if args[0] == "merge-base" else real(repo_path, args, **kw)
    monkeypatch.setattr(adapter, "_run", run)
    assert w.store.show(w.p("reader"), key)["work_exists"]["lanes"] is None


@pytest.mark.parametrize("reason", ["", "   ", "x" * 501])
def test_force_stale_blank_or_too_long_is_a_validation_refusal(w, repo, reason):
    key, = seed(w, [("A", [])])
    _merge_lane(repo, key)
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), key, force_stale=reason)
    assert exc.value.code == "validation"
    assert w.store._item(key)["state"] == "open"


def test_force_stale_over_a_newer_decision_is_audited(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("GEN-LOWS", description="per D-10"), defn("GOAL", ["GEN-LOWS"], kind="goal")])
    _decide(decisions, 12, "GEN-LOWS: premise changed")
    w.store.claim(w.p("l1main"), "demo:GEN-LOWS", force_stale="  D-12 read, plan unchanged  ")
    row = w.conn.execute("SELECT payload FROM events WHERE key=? AND type='claimed'",
                         ("demo:GEN-LOWS",)).fetchone()
    audit = json.loads(row["payload"])["force_stale"]
    assert audit["reason"] == "D-12 read, plan unchanged"
    assert audit["overrode"][0].startswith("premise outdated: D-12")


def test_cli_claim_sends_force_stale():
    from plangraph.keeper import cli, cli_verbs
    sent = []

    class Session:
        json_mode = True

        def call(self, method, verb, body=None):
            sent.append((method, verb, body))
            return {"key": body["key"], "epoch": 1}

    args = cli.build_parser().parse_args(["claim", "demo:A", "--force-stale", "why"])
    assert cli_verbs.cmd_claim(args, Session()) == 0
    assert sent == [("POST", "claim", {"key": "demo:A", "force_stale": "why"})]


def test_show_text_renders_the_evidence_ref():
    assert fmt._ev_ref({"ref": {"commit": "0123456789abcdef"}}) == "@01234567"
    assert fmt._ev_ref({"ref": {"decision": "D-7"}}) == "@D-7"
    assert fmt._ev_ref({"ref": {}}) == "" and fmt._ev_ref({}) == ""


def test_a_mention_is_not_a_supersede(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("A", description="per D-5"), defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 9, "keeps D-5; the old note superseded by D-12 is about B")
    assert w.store.show(w.p("reader"), "demo:A")["superseded_premises"] == {}
    _decide(decisions, 5, "WITHDRAWN by D-11: premise gone")
    assert w.store.show(w.p("reader"), "demo:A")["superseded_premises"] == {"D-5": "D-11"}


def test_brief_stays_under_the_cap_with_all_four_stale_reasons(w, repo, decisions):
    from plangraph.keeper.brief import BRIEF_CAP, brief_text
    w.store.create_items(w.p("router"), [
        defn("GEN-LOWS", description="per D-10 " + "x" * 900, done_criteria="y" * 500),
        defn("GOAL", ["GEN-LOWS"], kind="goal")])
    sha = _merge_lane(repo, "demo:GEN-LOWS")
    _decide(decisions, 11, "supersedes D-10")
    for did in range(100, 160):
        _decide(decisions, did, "GEN-LOWS premise moved again")
    w.clock.advance(9 * 86400)
    full = w.store.show(w.p("reader"), "demo:GEN-LOWS")["stale"]
    for part in ("untouched 9d", "premise D-10 superseded by D-11", "newer decision",
                 f"already merged {sha[:8]}"):
        assert part in full
    brief = w.store.show(w.p("reader"), "demo:GEN-LOWS", brief=True)
    assert brief["decisions"].count("D-") == 6 and brief["decisions"].endswith("D-159")
    assert brief["stale"].startswith("untouched 9d; premise D-10 superseded by D-11")
    assert brief["merged"].startswith(sha[:8])
    assert len(brief_text(brief)) <= BRIEF_CAP


def test_a_full_window_of_near_misses_is_unknown(w, repo, monkeypatch):
    from plangraph.keeper import context_sources
    key, = seed(w, [("A", [])])
    monkeypatch.setattr(context_sources, "_GREP_WINDOW", 2)
    for n in range(2):   # the grep line matches, the parsed trailer block does not
        _commit(repo, f"n{n}", f"work\n\nPlan-Item: {key}\n\nnot a trailer block")
    assert w.store.show(w.p("reader"), key)["work_exists"]["commits"] is None


def test_the_grep_pattern_is_a_posix_literal():
    from plangraph.keeper.context_sources import _ere
    assert _ere("GEN-SRV-W6.x") == r"GEN-SRV-W6\.x"


def test_lanes_are_unknown_when_the_ref_list_may_be_truncated(w, repo, monkeypatch):
    from plangraph.keeper import context_sources
    key, = seed(w, [("LANE-TRI", [])])
    _git(repo, "branch", "lane/lane-tri")
    monkeypatch.setattr(context_sources, "_REF_COUNT", 2)
    assert w.store.show(w.p("reader"), key)["work_exists"]["lanes"] is None


def test_a_retire_phrase_outside_the_decision_cell_is_ignored(w, decisions):
    w.store.create_items(w.p("router"), [
        defn("A", description="per D-5"), defn("GOAL", ["A"], kind="goal")])
    with open(decisions, "a", encoding="utf-8") as handle:
        handle.write("| D-5 | superseded by D-12 | keep the plan |\n")
    assert w.store.show(w.p("reader"), "demo:A")["superseded_premises"] == {}


def test_claim_guard_validates_the_reason_itself(w, repo):
    key, = seed(w, [("A", [])])
    _merge_lane(repo, key)
    with pytest.raises(Refused) as exc:
        w.store._claim_guard(w.store._item(key), "  ")
    assert exc.value.code == "validation"


def test_ready_checks_the_repo_before_the_cut_to_n(w, repo):
    keys = seed(w, [("A", []), ("B", [])])
    w.store.set_priority(w.p("router"), keys[0], 5)
    _merge_lane(repo, keys[0])
    rows = w.store._ready_items(project="demo", n=1)
    assert [r["key"] for r in rows] == ["demo:B"]


def test_a_decisions_source_without_a_file_is_still_read(w):
    class Source:
        def _rows(self):
            return {"D-12": ["| D-12 | demo | GEN-LOWS: premise changed |"]}

    w.store.create_items(w.p("router"), [
        defn("GEN-LOWS", description="per D-10"), defn("GOAL", ["GEN-LOWS"], kind="goal")])
    w.store.ctx["decisions"] = Source()
    assert w.store.show(w.p("reader"), "demo:GEN-LOWS")["newer_decisions"] == ["D-12"]
