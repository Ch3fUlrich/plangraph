"""PR 878 review regressions: authority, freshness and witnessed seats."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_git import GitAdapter
from test_keeper_close_merged import HEAD, MERGE, unseated_w
from test_keeper_close_merged_router_path import mirrored_reviews
from test_keeper_waiver_validation import historical_waiver_fixture


@pytest.mark.parametrize("name", [
    "docs/KEEPER.md", "docs/keeper/keeper-decision-close.md",
    "changelog.d/KEEPER-MERGED-OPEN-RECONCILE-1.md",
])
def test_go_stamp_docs_name_the_supported_forms(name):
    text = (Path(__file__).resolve().parents[1] / name).read_text()
    assert "(HH:MMZ)" in text and "(HH:MxZ)" in text
    assert "all decision-log timestamp prefixes" not in text
    assert "short and approximate stamps" not in text


@pytest.mark.parametrize("text", [
    "TEST_GAMING: yes\nTEST_GAMING: no\nVERDICT: PASS",
    "TEST_GAMING: no\nTEST_GAMING: yes\nVERDICT: PASS",
    "TEST_GAMING: no\nTEST_GAMING: no\nVERDICT: PASS",
])
def test_duplicate_gaming_lines_refuse(unseated_w, tmp_path, text):
    world = unseated_w
    root = mirrored_reviews(world, tmp_path)
    path = root / "fixture/run-two/record.json"
    record = json.loads(path.read_text())
    record["stdout_tail"] = text
    path.write_text(json.dumps(record))
    with pytest.raises(Refused, match="NO-WITNESS"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504")
    assert world.store._item("demo:H")["state"] == "in_review"


@pytest.mark.parametrize("finish", ["length", "cancelled", "", None])
def test_completion_finish_cannot_be_masked(unseated_w, tmp_path, finish):
    world = unseated_w
    root = mirrored_reviews(world, tmp_path)
    path = root / "fixture/run-two/record.json"
    record = json.loads(path.read_text())
    record["completion"]["finish_reason"] = finish
    path.write_text(json.dumps(record))
    with pytest.raises(Refused, match="NO-WITNESS"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504")


@pytest.mark.parametrize("risk", ["auth", "secrets"])
def test_waiver_records_overridden_seat_rule(unseated_w, monkeypatch, risk):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    world.store.put_card(world.p("router"), "demo:H", {"outcome": "fixture", "risk_tags": [risk]})
    result = world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert result["reviews_passed"] == 0
    ref = world.store._evidence("demo:H")[-1]["ref"]
    assert ref["review_tier"] == "high"
    assert ref["risk_tags"] == [risk]
    assert ref["seat_rule_waived_by"] == "D-6000"
    assert world.store.verify_integrity() == []


def test_families_and_waiver_refuse(unseated_w, monkeypatch):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    with pytest.raises(Refused, match="families_with_waive_seats"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", [], waive_seats="D-6000")
    assert world.store._item("demo:H")["state"] == "in_review"


def test_synthetic_bundle_is_not_a_later_citation(unseated_w, tmp_path):
    world = unseated_w
    mirrored_reviews(world, tmp_path)
    world.conn.execute("UPDATE items SET run_ids=? WHERE key='demo:H'",
                       (json.dumps(["merged-D-5504"]),))
    assert world.store.close_merged(world.p("router"), "demo:H", "D-5504")["state"] == "done"
    assert world.store.verify_integrity() == []


@pytest.mark.parametrize("failed", [False, True])
def test_complete_pr_scan_refreshes_or_refuses(unseated_w, monkeypatch, failed):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    git = GitAdapter({"demo": {"path": "fixture-repo", "ref": "origin/main"}})
    refreshed = []
    def run(repo, args, **kwargs):
        if args[0] == "fetch":
            refreshed.append(args)
            return SimpleNamespace(returncode=int(failed), stdout="")
        if "--format=%cI" in args:
            return SimpleNamespace(returncode=0, stdout="2026-11-12T12:00:00Z")
        return SimpleNamespace(returncode=0, stdout=f"{'2' * 40} {HEAD}")
    monkeypatch.setattr(git, "_run", run)
    monkeypatch.setattr(git, "merge_commit_of", lambda *a: (MERGE, "fixture"))
    world.store.ctx["git"] = git
    original = world.store._merged_open_prs_checked
    def checked(*args):
        # Any scan that reports complete must first refresh; the GO lookup is
        # intentionally allowed to read its narrower local proof beforehand.
        result = original(*args)
        assert refreshed
        return result
    monkeypatch.setattr(world.store, "_merged_open_prs_checked", checked)
    if failed:
        with pytest.raises(Refused, match="merged_default_ref_refresh_failed"):
            world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    else:
        assert world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")["state"] == "done"
    assert refreshed


def test_refresh_exposes_previously_unseen_pr(unseated_w, monkeypatch):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    git = GitAdapter({"demo": {"path": "fixture-repo", "ref": "origin/main"}})
    refreshed = []
    other = "f" * 40
    def run(repo, args, **kwargs):
        if args[0] == "fetch":
            refreshed.append(args)
            return SimpleNamespace(returncode=0, stdout="")
        if "--format=%cI" in args:
            return SimpleNamespace(returncode=0, stdout="2026-11-12T12:00:00Z")
        return SimpleNamespace(returncode=0, stdout=f"{'2' * 40} {args[-1]}")
    def commits(*args, **kwargs):
        return [{"sha": HEAD, "merge": {"pr": 1}}] + (
            [{"sha": other, "merge": {"pr": 900}}] if refreshed else [])
    monkeypatch.setattr(git, "_run", run)
    monkeypatch.setattr(git, "merge_commit_of", lambda repo, sha, ref: (sha, "fixture"))
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", commits)
    world.store.ctx["git"] = git
    with pytest.raises(Refused, match="merged_pr_go_missing"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert refreshed == [["fetch", "--quiet", "--", "origin",
                          "+refs/heads/main:refs/remotes/origin/main"]]
    assert world.store._item("demo:H")["state"] == "in_review"


@pytest.mark.parametrize("project,key", [("other", "other:H"), ("demo", "demo:OTHER")])
def test_another_items_go_with_same_head_refuses(unseated_w, monkeypatch, project, key):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    other_head = "f" * 40
    world.decision_path.write_text(world.decision_path.read_text() +
        f"| D-6001 | {project} | GO #2 {key}; GO-HEAD {other_head} | ROUTER | 2026-11-12 |\n")
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", lambda *a, **kw: [
        {"sha": HEAD, "merge": {"pr": 1}}, {"sha": other_head, "merge": {"pr": 2}}])
    git = world.store.ctx["git"]
    monkeypatch.setattr(git, "merge_commit_of", lambda repo, sha, ref: (sha, "fixture"))
    monkeypatch.setattr(git, "_run", lambda repo, args: SimpleNamespace(returncode=0,
        stdout="2026-11-12T12:00:00Z" if "--format=%cI" in args else f"{'2' * 40} {args[-1]}"))
    with pytest.raises(Refused, match="merged_pr_go_missing"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert world.store._item("demo:H")["state"] == "in_review"


@pytest.mark.parametrize("project,key", [("other", "other:H"), ("demo", "demo:OTHER")])
def test_selected_go_of_another_item_refuses(unseated_w, monkeypatch, project, key):
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    text = world.decision_path.read_text().replace(
        f"| D-5504 | demo | GO #1; GO-HEAD {HEAD}",
        f"| D-5504 | {project} | GO #1 {key}; GO-HEAD {HEAD}")
    world.decision_path.write_text(text)
    with pytest.raises(Refused, match="merged_pr_go_scope_mismatch"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert world.store._item("demo:H")["state"] == "in_review"
