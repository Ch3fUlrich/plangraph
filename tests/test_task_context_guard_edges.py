"""TASK-CONTEXT-GUARD (D-3215) edge cases: unknown facts, bounds, override audit.

Shares the real throw-away repository and decisions-log fixtures of
``test_task_context_guard``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from keeper_util import defn, seed
from plangraph.keeper import fmt
from plangraph.keeper.errors import Refused
from test_task_context_guard import (  # noqa: F401  (fixtures)
    _commit, _decide, _git, _merge_lane, _mk, _newer, _refuses, decisions, repo, w)

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


def test_merge_of_ignores_older_unrelated_merges(w, repo, merge_history):
    from plangraph.keeper import context_sources
    sha, expected, pr = merge_history["plain"]
    merge = context_sources._merge_of(w.store.ctx["git"], str(repo), "main", sha)
    assert merge == {"sha": expected, "subject": "plain (#899)", "pr": pr}


@pytest.mark.parametrize("failure", ["ancestry", "first-parent"])
def test_merge_of_is_unknown_when_a_walk_fails(
        w, repo, merge_history, monkeypatch, failure):
    from plangraph.keeper import context_sources
    adapter = w.store.ctx["git"]
    real = adapter._run

    def run(repo_path, args, **kw):
        flag = "--ancestry-path" if failure == "ancestry" else "--first-parent"
        if args[0] == "rev-list" and flag in args:
            return _Proc(128)
        return real(repo_path, args, **kw)

    monkeypatch.setattr(adapter, "_run", run)
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


@pytest.mark.parametrize("status,expected", [
    ("CLOSED", []), ("OPEN", ["lane/lane-two"]), ("MOVED", ["lane/lane-two"]),
    ("API_ERROR", None), ("GIT_ERROR", None),
])
def test_close_lane_requires_closed_pr_at_exact_head(w, repo, monkeypatch, status, expected):
    import subprocess
    key, = seed(w, [("LANE-TWO", [])])
    branch = "lane/lane-two"
    _git(repo, "checkout", "-qb", branch)
    head = _commit(repo, "wip", "unmerged")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "remote", "add", "origin", "https://github.com/example/repo")
    adapter = w.store.ctx["git"]
    real_git, real_run = adapter._run, subprocess.run

    def git(repo_path, args, **kwargs):
        if args[0] == "fetch":
            return _Proc(0)
        if args[0] == "ls-remote":
            return _Proc(1 if status == "GIT_ERROR" else 0,
                         f"{head if status != 'MOVED' else 'a' * 40}\trefs/pull/7/head\n")
        return real_git(repo_path, args, **kwargs)

    def run(args, **kwargs):
        if args[0] == "gh":
            return _Proc(1 if status == "API_ERROR" else 0, json.dumps([
                {"number": 7, "state": "OPEN" if status == "OPEN" else "CLOSED",
                 "headRefOid": head, "isCrossRepository": False}]))
        return real_run(args, **kwargs)

    monkeypatch.setattr(adapter, "_run", git)
    monkeypatch.setattr(subprocess, "run", run)
    work = w.store.show(w.p("reader"), key)["work_exists"]
    assert work["lanes"] == expected
    if status == "CLOSED":
        assert work["closed_pr_lanes"] == [{"branch": branch, "pr": 7}]
    with pytest.raises(Refused, match=("merged_on_default_missing" if status == "CLOSED"
                                      else "unmerged_work_or_lanes_unknown")):
        w.store._merged_waiver_only(w.store._item(key), "D-1", w.store.now())


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


@pytest.mark.parametrize("pr_project", ["demo", "data"])
def test_open_pull_trailers_block_ready_and_all_claims_until_close(
        w, repo, tmp_path, monkeypatch, pr_project):
    import subprocess

    from plangraph.keeper import context_sources
    from plangraph.keeper.evidence_git import GitAdapter

    key, = seed(w, [("A", [])])
    data = tmp_path / "data"
    _git(tmp_path, "clone", "-q", str(repo), str(data))
    _git(data, "config", "user.email", "t@example.invalid")
    _git(data, "config", "user.name", "t")
    paths = {"demo": str(repo), "data": str(data)}
    carrying = paths[pr_project]
    _git(carrying, "checkout", "-qb", "train/fixture")
    head = _commit(Path(carrying), "work",
                   f"work\n\nPlan-Item: {key}")
    _git(carrying, "checkout", "-q", "main")
    for name, path in paths.items():
        if name == "data":
            _git(path, "remote", "remove", "origin")
        _git(path, "remote", "add", "origin", f"https://github.com/example/{name}")
        _git(path, "update-ref", "refs/remotes/origin/main", "main")
    adapter = GitAdapter(paths)
    w.store.ctx["git"] = adapter
    real_git, real_run = adapter._run, subprocess.run
    opened = True
    available = True

    def git(path, args, **kwargs):
        return _Proc(0) if args[0] == "fetch" else real_git(path, args, **kwargs)

    def run(args, **kwargs):
        if args[:3] == ["gh", "pr", "list"]:
            if not available and kwargs["cwd"] == carrying:
                return _Proc(1)
            return _Proc(0, json.dumps([
                {"number": 1100, "headRefOid": head, "headRefName": "train/fixture"}
            ] if kwargs["cwd"] == carrying and opened else []))
        return real_run(args, **kwargs)

    monkeypatch.setattr(adapter, "_run", git)
    monkeypatch.setattr(subprocess, "run", run)
    l1 = w.p("l1main")
    ready = w.store.ready_data(l1)
    assert key not in [row["key"] for row in ready["items"]]
    row, = [row for row in ready["in_progress"] if row["key"] == key]
    assert (row["state"], row["prs"]) == ("in_review", [1100])
    with pytest.raises(Refused, match="carried by #1100"):
        w.store.claim(l1, key, force_stale="approved retry")
    with pytest.raises(Refused, match="carried by #1100"):
        w.store.worker_claim_lease(w.p("system"), key)
    assert w.store._item(key)["state"] == "open"

    available = False
    with pytest.raises(Refused, match="carried by #1100"):
        w.store.claim(l1, key)
    available = True
    opened = False
    context_sources.work_index(adapter, pr_project, fresh=True)
    ready = w.store.ready_data(l1)
    assert key in [row["key"] for row in ready["items"]]
    assert w.store.claim(l1, key)["epoch"] == 1


@pytest.mark.parametrize("failure", ["pr-list", "origin-fetch"])
def test_unverified_pr_inventory_in_another_repo_blocks_ready_and_claims(
        w, repo, tmp_path, monkeypatch, failure):
    import subprocess

    from plangraph.keeper.auth import add_principal, authenticate
    from plangraph.keeper.evidence_git import GitAdapter

    key, = seed(w, [("A", [])])
    phase = authenticate(w.conn, add_principal(w.conn, "phase", "phase-worker", ["demo"]))
    data = tmp_path / "data"
    _git(tmp_path, "clone", "-q", str(repo), str(data))
    _git(data, "remote", "set-url", "origin", "https://github.com/example/data")
    adapter = GitAdapter({"demo": str(repo), "data": str(data)})
    w.store.ctx["git"] = adapter
    real_git, real_run = adapter._run, subprocess.run

    def git(path, args, **kwargs):
        if args == ["fetch", "--no-tags", "origin"]:
            assert path == str(data)
            return _Proc(int(failure == "origin-fetch"))
        return real_git(path, args, **kwargs)

    def run(args, **kwargs):
        if args[:3] == ["gh", "pr", "list"]:
            assert kwargs["cwd"] == str(data)
            return _Proc(int(failure == "pr-list"), "[]")
        return real_run(args, **kwargs)

    monkeypatch.setattr(adapter, "_run", git)
    monkeypatch.setattr(subprocess, "run", run)
    assert not getattr(adapter, "_context_work_index", {})
    message = "open PR inventory unavailable: data"
    with pytest.raises(Refused, match=f"^bad_state: {message}$"):
        w.store.claim(phase, key, force_stale="checked")
    ready = w.store.ready_data(w.p("reader"), project="demo")
    assert ready["items"] == []
    row, = ready["unknown"]
    assert (row["key"], row["ready_status"], row["work_index"]["reason"]) == (key, "unknown", message)
    with pytest.raises(Refused, match=message):
        w.store.claim(w.p("l1main"), key)
    assert w.store._item(key)["epoch"] == 0


@pytest.mark.parametrize("source", ["trailer", "merge-body", "foreign-bare"])
def test_ready_and_claim_check_merged_work_in_all_repositories(w, repo, tmp_path, source):
    from plangraph.keeper.evidence_git import GitAdapter

    data = tmp_path / "data"
    _git(tmp_path, "clone", "-q", "--no-local", str(repo), str(data))
    _git(data, "remote", "remove", "origin")
    _git(data, "config", "user.email", "t@example.invalid")
    _git(data, "config", "user.name", "t")
    _git(data, "config", "commit.gpgsign", "false")
    key, = seed(w, [("CROSS-REPO", [])])
    if source == "merge-body":
        _git(data, "checkout", "-qb", "part")
        _commit(data, "delivery", "work")
        _git(data, "checkout", "-q", "main")
        _git(data, "merge", "-q", "--no-ff", "-m",
             f"Land train (#7)\n\nPlan-Item: {key}\n\nTrain summary follows.", "part")
    else:
        trailer = key if source == "trailer" else "CROSS-REPO"
        _commit(data, "delivery", f"work\n\nPlan-Item: {trailer}")
    w.store.ctx["git"] = GitAdapter({"demo": str(repo), "data-repo": str(data)})

    ready = w.store.ready_data(w.p("reader"), "demo")
    if source == "foreign-bare":
        assert [row["key"] for row in ready["items"]] == [key]
        assert w.store.claim(w.p("l1main"), key)["epoch"] == 1
    else:
        assert ready["items"] == []
        assert [row["key"] for row in ready["needs_reconcile"]] == [key]
        with pytest.raises(Refused, match="stale: already merged"):
            w.store.claim(w.p("l1main"), key)


@pytest.mark.parametrize("landed,named,pr", [(True, True, 55), (True, False, 55),
                                             (False, True, 55), (True, True, 56),
                                             (True, "excluded", 55)])
def test_go_part_table_excludes_only_keys_in_landed_parts(w, repo, decisions, landed, named, pr):
    _git(repo, "checkout", "-qb", "part")
    part = _commit(repo, "part-work", "work with no Plan-Item trailer")
    _git(repo, "checkout", "-qb", "train")
    head = _commit(repo, "train-work", "prepare the train")
    _git(repo, "checkout", "-q", "main")
    if landed:
        _git(repo, "merge", "-q", "--no-ff", "-m", "Land train (#55)", "train")
    table_key = "demo:GO-PART" if named is True else "demo:OTHER"
    if named == "excluded":
        table_key += " (excludes demo:GO-PART)"
    _decide(decisions, 55, f"GO #{pr}; GO-HEAD {head}; Per-part table: Batch 8, part head {part[:9]}, keys {table_key}")
    key, = seed(w, [("GO-PART", [])])

    ready = w.store.ready_data(w.p("reader"), "demo")
    if landed and named is True and pr == 55:
        from plangraph.keeper.api_routes import ROLE_CHECKED_VERBS

        w.store.warm_merge_scan(w.store.merge_candidates())
        report = ROLE_CHECKED_VERBS["close_merged_auto"](
            w.store, w.p("system"), {"apply": False, "watch": True}, None)
        assert report["merged_not_done"] == 1
        assert [(row["key"], row["reason"].split(":")[1].strip()) for row in report["listed"]] == [
            (key, "decision_merge_only")]
        assert report["closed"] == []
        assert ready["items"] == []
        assert [row["key"] for row in ready["needs_reconcile"]] == [key]
        with pytest.raises(Refused, match=r"stale: already merged: GO part D-55"):
            w.store.claim(w.p("l1main"), key)
    else:
        assert [row["key"] for row in ready["items"]] == [key]
        assert w.store.claim(w.p("l1main"), key)["epoch"] == 1


@pytest.mark.parametrize("qualified", [True, False])
def test_hashed_close_decision_requires_an_exact_key_and_landed_merge(w, repo, decisions, qualified):
    _git(repo, "checkout", "-qb", "part")
    _commit(repo, "legacy-work", "work before item trailers")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Land legacy work (#56)", "part")
    merged = _git(repo, "rev-parse", "HEAD")
    scope = f"demo:CLOSE-TOKEN@{merged}" if qualified else "demo:CLOSE-TOKEN"
    _decide(decisions, 56, f"Router checked merged delivery. Closes: {scope}")
    key, = seed(w, [("CLOSE-TOKEN", [])])
    ready = w.store.ready_data(w.p("reader"), "demo")
    if qualified:
        assert ready["items"] == []
        assert [row["key"] for row in ready["needs_reconcile"]] == [key]
        with pytest.raises(Refused, match="already merged: Close decision D-56"):
            w.store.claim(w.p("l1main"), key)
    else:
        assert [row["key"] for row in ready["items"]] == [key]


# ------------------------------- stale-premise watermark: metadata, legacy notes, floor
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


def test_a_credential_label_in_a_sibling_metadata_field_covers_the_value(w, tmp_path):
    from plangraph.keeper.importer import load_dir
    plan = tmp_path / "plan"
    plan.mkdir()
    secret = "svc" + "acct/" + "Zq9xk2LmPw"
    (plan / "p.yaml").write_text(
        "items:\n"
        f"- {{project: demo, id: A, title: a, category: 'the password is', session: '{secret}', owner: tester,"
        " track: T0, estimate_h: 1, kind: lane}\n", encoding="utf-8")
    with pytest.raises(Refused) as exc:
        load_dir(plan)
    assert "credential" in exc.value.msg and secret not in exc.value.msg


def test_editing_the_authored_part_of_a_legacy_note_does_not_publish_its_folded_metadata(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("L", note="plain note"))
    w.conn.execute("UPDATE items SET note=? WHERE key='demo:L'",
                   ("plain note\nsession: D-999999",))                   # as an old Keeper left it
    w.store.edit_definition(w.p("router"), "demo:L", {"note": "reworded note\nsession: D-999999"})
    assert w.store._decision_clock()["marks"][-1] == 5800
    w.store.edit_definition(w.p("router"), "demo:L", {"note": "reworded again per D-5801\nsession: D-999999"})
    assert w.store._decision_clock()["marks"][-1] == 5801          # an id the edit adds is evidence
    _mk(w, defn("Z"))
    _decide(decisions, 5900, "demo:Z scope changed")
    assert "D-5900" in _refuses(w, "demo:Z")


def test_rehoming_an_item_keeps_its_metadata_for_the_export_and_does_not_republish_its_note(w, decisions, tmp_path):
    from plangraph.keeper.importer import export_yaml, load_dir
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("A", session="sess-9", category="infra", similar=["demo:B"], supersedes="demo:OLD"))
    w.conn.execute("UPDATE items SET note=? WHERE key='demo:A'", ("n\nsession: D-999999",))
    w.store.rehome(w.p("router"), "demo:A", "plangraph", "workstation")
    out = tmp_path / "export"
    export_yaml(w.store, out)
    moved = next(i for i in load_dir(out) if i["key"] == "plangraph:A")
    assert [moved[n] for n in ("session", "category", "similar", "supersedes")] == [
        "sess-9", "infra", ["demo:B"], "demo:OLD"]
    assert w.store._decision_clock()["marks"][-1] == 5800           # the carried note is no new evidence


def test_a_reconfirmation_is_the_watermark_of_an_item_that_cites_nothing(w, decisions):
    _mk(w, defn("U"))
    _decide(decisions, 10, "demo:U scope changed")
    assert _newer(w, "demo:U") == []                  # nothing to read the decision against: unknown
    w.store.reconfirm(w.p("router"), "demo:U", "D-10")
    _decide(decisions, 11, "demo:U scope changed again")
    assert _newer(w, "demo:U") == ["D-11"]
    assert "D-11" in _refuses(w, "demo:U")
    with pytest.raises(Refused) as exc:
        w.store.worker_claim_lease(w.p("system"), "demo:U")
    assert "premise outdated: D-11" in exc.value.msg
    ready = {r["key"]: r["ready_status"] for r in w.store._ready_items(project="demo", n=None)}
    assert ready["demo:U"] == "needs_reconcile"


def test_metadata_an_older_keeper_folded_into_a_note_is_not_premise_text(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("L"), defn("M"))
    for key, note in (("demo:L", "session: D-999999"),
                      ("demo:M", "authored per D-5\nsession: D-999999")):       # as an older Keeper left them
        w.conn.execute("UPDATE items SET note=? WHERE key=?", (note, key))
    _decide(decisions, 5, "an old plan")
    _decide(decisions, 9, "supersedes D-5 for demo")
    _decide(decisions, 5900, "demo:L scope changed")
    assert "D-5900" in _refuses(w, "demo:L")           # the legacy session id is not a citation
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:M")
    assert "premise D-5 superseded by D-9" in exc.value.msg      # the authored part still is


def test_an_older_keepers_note_is_exported_and_imported_as_fields(w, decisions, tmp_path):
    from plangraph.keeper.importer import export_yaml, load_dir
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("L"), defn("M"))
    w.conn.execute("UPDATE items SET note=? WHERE key='demo:L'", ("session: D-999999",))
    w.conn.execute("UPDATE items SET note=? WHERE key='demo:M'", ('plain note\nsimilar: ["demo:L"]',))
    out = tmp_path / "export"
    export_yaml(w.store, out)
    items = {i["id"]: i for i in load_dir(out)}
    assert (items["L"]["session"], items["L"].get("note")) == ("D-999999", None)
    assert (items["M"]["note"], items["M"]["similar"]) == ("plain note", ["demo:L"])


def test_a_note_written_by_hand_is_authored_text_at_the_door(w, decisions, tmp_path):
    from plangraph.keeper.importer import load_dir
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "p.yaml").write_text(
        "items:\n- {project: demo, id: H, title: h, note: \"plain\\ncategory: see D-5 for context\", owner: tester,"
        " track: T0, estimate_h: 1, kind: lane}\n", encoding="utf-8")
    [hand] = load_dir(plan)
    assert hand["note"] == "plain\ncategory: see D-5 for context" and "category" not in hand
    _mk(w, defn("A", note="plain\ncategory: see D-5 for context"))        # and so is a created one
    _decide(decisions, 5, "an old plan")
    _decide(decisions, 9, "supersedes D-5 for demo")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:A")
    assert "premise D-5 superseded by D-9" in exc.value.msg


@pytest.mark.parametrize("legacy", [
    "session: D-999999\nworkstreams: [a, b]",
    "category: first line\nsecond line cites D-999999\nthird line\nsession: D-999999",
    "authored per D-8\nsimilar: [\"D-999999\"]\nsupersedes: D-999999\nzeta: kept",
])
def test_every_shape_of_an_older_keepers_fold_is_read_as_metadata(w, decisions, legacy):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("L"))
    w.conn.execute("UPDATE items SET note=? WHERE key='demo:L'", (legacy,))        # as an older Keeper left it
    _decide(decisions, 5900, "demo:L scope changed")
    assert "D-5900" in _refuses(w, "demo:L")
    assert "D-999999" not in str(w.store._decision_clock()["marks"])


def test_an_edit_that_adds_a_title_citation_does_not_make_a_legacy_note_authored(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("L", title="plain title"))
    w.conn.execute("UPDATE items SET note=? WHERE key='demo:L'", ("session: D-999999",))
    w.store.edit_definition(w.p("router"), "demo:L", {"title": "plain title per D-5801"})
    _decide(decisions, 5900, "demo:L scope changed")
    assert "D-5900" in _refuses(w, "demo:L")


def test_rehome_keeps_the_provenance_of_an_authored_note_and_of_a_legacy_one(w, decisions):
    _mk(w, defn("P", note="authored\ncategory: see D-5 for context"))
    _mk(w, defn("Q"))
    w.conn.execute("UPDATE items SET note=? WHERE key='demo:Q'", ("session: D-999999",))
    _decide(decisions, 5, "an old plan")
    _decide(decisions, 9, "supersedes D-5 for demo")
    _decide(decisions, 5900, "plangraph:Q scope changed")
    _mk(w, defn("C", description="per D-5800"))
    for key in ("demo:P", "demo:Q"):
        w.store.rehome(w.p("router"), key, "plangraph", "workstation")
    with pytest.raises(Refused) as exc:
        w.store.worker_claim_lease(w.p("system"), "plangraph:P")
    assert "premise D-5 superseded by D-9" in exc.value.msg                   # authored: still cited
    with pytest.raises(Refused) as exc:
        w.store.worker_claim_lease(w.p("system"), "plangraph:Q")
    assert "D-5900" in exc.value.msg                                          # legacy: still not cited


def test_a_credential_label_in_a_still_folded_extension_field_covers_the_metadata_value(w):
    secret = "svc" + "acct/" + "Zq9xk2LmPw"
    with pytest.raises(Refused) as exc:
        _mk(w, defn("A", label="the password is", session=secret))
    assert "credential" in exc.value.msg and secret not in exc.value.msg
