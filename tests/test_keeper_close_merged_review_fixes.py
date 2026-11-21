"""PR 878 review regressions: authority, freshness and witnessed seats."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence import VERIFIERS
from plangraph.keeper.evidence_git import GitAdapter
from test_keeper_close_merged import HEAD, MERGE, MergedGit, unseated_w, w
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
def test_waiver_refuses_credential_lane_seat_override(unseated_w, monkeypatch, risk):
    """Auth/secrets seat rules cannot be overridden or recorded as waived."""
    world = unseated_w
    historical_waiver_fixture(world, monkeypatch)
    world.store.put_card(world.p("router"), "demo:H", {"outcome": "fixture", "risk_tags": [risk]})
    before = world.store._evidence("demo:H")
    with pytest.raises(Refused, match="insufficient_non_anthropic_seat_families: need 2"):
        world.store.close_merged(world.p("router"), "demo:H", "D-5504", waive_seats="D-6000")
    assert world.store._item("demo:H")["state"] == "in_review"
    assert world.store._evidence("demo:H") == before
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
        if args[0] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout=MERGE)
        if "--format=%cI" in args:
            return SimpleNamespace(returncode=0, stdout="2026-11-12T12:00:00Z")
        return SimpleNamespace(returncode=0, stdout=f"{'2' * 40} {HEAD}")
    monkeypatch.setattr(git, "_run", run)
    monkeypatch.setattr(git, "merge_commit_of", lambda *a: (MERGE, "fixture"))
    monkeypatch.setattr(git, "deploy_changes", lambda *a: [])
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
        if args[0] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout=MERGE)
        if "--format=%cI" in args:
            return SimpleNamespace(returncode=0, stdout="2026-11-12T12:00:00Z")
        return SimpleNamespace(returncode=0, stdout=f"{'2' * 40} {args[-1]}")
    def commits(*args, **kwargs):
        return [{"sha": HEAD, "merge": {"pr": 1}}] + (
            [{"sha": other, "merge": {"pr": 900}}] if refreshed else [])
    monkeypatch.setattr(git, "_run", run)
    monkeypatch.setattr(git, "merge_commit_of", lambda repo, sha, ref: (sha, "fixture"))
    monkeypatch.setattr(git, "deploy_changes", lambda *a: [])
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


# ----- LE-MERGED-UNIT-NEVER-DEPLOYED: a merged unit is not done until active on every host

def _ships(w, monkeypatch, paths, hosts="alpha, beta"):
    git = MergedGit()
    git.ref_commit = lambda repo, ref: "1" * 40
    git.deploy_changes = lambda repo, sha, ref: [
        p if isinstance(p, tuple) else ("A", p) for p in paths]
    w.store.ctx = {**w.store.ctx, "git": git}
    w.store.verifiers["deploy"] = VERIFIERS["deploy"]
    w.conn.execute("UPDATE items SET description=? WHERE key='demo:A'",
                   (f"Why: x\nHow: y\nDeploy-Hosts: {hosts}\n" if hosts else "Why: x\nHow: y\n",))


def _deploy(w, host, unit, is_active="active"):
    return w.store.add_evidence_router(w.p("router"), "demo:A", "deploy",
                                       {"host": host, "unit": unit, "is_active": is_active})


UNIT = ["deploy/systemd/pg-disk-hygiene.service", "deploy/systemd/pg-disk-hygiene.timer"]


def test_shipped_unit_without_deploy_proof_stays_open_and_alerts(w, monkeypatch):
    _ships(w, monkeypatch, UNIT)
    out = w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])
    assert out["closed"] is False and out["reason"] == "deploy_pending"
    assert "alpha:pg-disk-hygiene.timer" in out["missing"]
    assert w.store._item("demo:A")["state"] == "in_review"
    alerts = w.store.alerts(w.p("reader"))
    assert [f["code"] for f in alerts["findings"] if f["key"] == "demo:A"] == ["deploy_pending"]


def test_unit_closes_once_every_named_host_reports_active(w, monkeypatch):
    _ships(w, monkeypatch, UNIT)
    for host in ("alpha", "beta"):
        for unit in ("pg-disk-hygiene.service", "pg-disk-hygiene.timer"):
            assert _deploy(w, host, unit)["verified"]
    out = w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])
    assert out["state"] == "done"


def test_one_host_missing_keeps_the_item_open(w, monkeypatch):
    _ships(w, monkeypatch, UNIT)
    for unit in ("pg-disk-hygiene.service", "pg-disk-hygiene.timer"):
        _deploy(w, "alpha", unit)
    out = w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])
    assert out["missing"] == ["beta:pg-disk-hygiene.service", "beta:pg-disk-hygiene.timer"]


def test_inactive_report_is_not_proof_and_unnamed_hosts_stay_pending(w, monkeypatch):
    _ships(w, monkeypatch, UNIT, hosts="")
    out = w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])
    assert out["missing"] == ["(hosts unnamed)"]
    assert _deploy(w, "alpha", "x.service", "inactive")["verified"] is False


def test_host_binary_needs_installed_proof_and_other_diffs_close_as_before(w, monkeypatch):
    _ships(w, monkeypatch, ["rust/pg-disk/src/main.rs"], hosts="alpha")
    assert w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])["missing"] == [
        "alpha:bin:pg-disk"]
    _ships(w, monkeypatch, ["src/plangraph/keeper/store.py", "docs/KEEPER.md"])
    assert w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])["state"] == "done"


def test_deploy_targets_count_a_binary_only_when_its_entry_point_is_added():
    from plangraph.keeper.evidence import deploy_targets
    assert deploy_targets([("M", "rust/pg-disk/src/main.rs"), ("D", "rust/pg-old/src/bin/tool.rs")]) == []
    assert deploy_targets([("A", "rust/pg-disk/src/main.rs"), ("A", "rust/pg/src/bin/tool.rs")]) == [
        "bin:pg-disk", "bin:tool"]
    assert deploy_targets([("M", "deploy/systemd/a.service")]) == ["a.service"]
    assert deploy_targets(["rust/pg-disk/src/main.rs"]) == ["bin:pg-disk"]


def _pr_repo(tmp_path, follow_up_files, tagged_files=None, *, main_after=None, tag_merge=False):
    """A repo whose PR has a tagged commit and an untagged follow-up that adds `follow_up_files`."""
    import os
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True, env=env).stdout.strip()
    repo = tmp_path / "pr-repo"
    (repo / "rust/pg-old/src").mkdir(parents=True)
    (repo / "rust/pg-old/src/main.rs").write_text("fn main() {}\n")
    git("-c", "init.defaultBranch=main", "init", "-q")
    git("add", "-A")
    git("commit", "-m", "seed")
    git("checkout", "-q", "-b", "batch")
    (repo / "NOTES.md").write_text("tagged work\n")
    for path, text in (tagged_files or {}).items():
        if text is None:
            (repo / path).unlink()
        else:
            (repo / path).write_text(text)
    git("add", "-A")
    git("commit", "-m", "work\n\nPlan-Item: demo:A")
    for path, text in follow_up_files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            (repo / path).unlink()
        else:
            (repo / path).write_text(text)
    git("add", "-A")
    git("commit", "-m", "follow-up without a trailer")
    tip = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    for path, text in (main_after or {}).items():  # the default branch moves on after the branch point
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
        git("add", "-A")
        git("commit", "-m", f"main gains {path}")
    subject = "batch (#1078)" + ("\n\nPlan-Item: demo:A" if tag_merge else "")
    git("merge", "-q", "--no-ff", "-m", subject, "batch")
    return repo, tip


def _close_pr_repo(w, tmp_path, monkeypatch, follow_up_files, tagged_files=None, **repo_args):
    repo, tip = _pr_repo(tmp_path, follow_up_files, tagged_files, **repo_args)
    from plangraph.keeper.evidence_git import GitAdapter
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", w.real_plan_item_commits)
    monkeypatch.setattr("plangraph.keeper.context_sources.open_lanes", lambda *a, **kw: [])
    w.store.ctx["git"] = GitAdapter({"demo": {"path": str(repo), "ref": "main"}})
    w.store.verifiers["deploy"] = VERIFIERS["deploy"]
    w.conn.execute("UPDATE items SET state='open', description=? WHERE key='demo:A'",
                   ("Why: x\nHow: y\nDeploy-Hosts: alpha\n",))
    w.decision_path.write_text(f"| D-6395 | demo | GO #1078 demo:A; GO-HEAD {tip} | ROUTER |\n")
    return w.store.close_merged(w.p("router"), "demo:A", "D-6395", ["google"],
                                from_open=True, apply=True)


def test_an_untagged_follow_up_of_the_same_pr_that_adds_a_unit_needs_deploy_proof(w, tmp_path, monkeypatch):
    out = _close_pr_repo(w, tmp_path, monkeypatch,
                         {"deploy/systemd/new.service": "[Service]\n"})
    assert out["closed"] is False and out["reason"] == "deploy_pending"
    assert out["missing"] == ["alpha:new.service"]


def test_editing_an_existing_binary_entry_point_ships_no_new_host_binary(w, tmp_path, monkeypatch):
    out = _close_pr_repo(w, tmp_path, monkeypatch,
                         {"rust/pg-old/src/main.rs": "fn main() { run(); }\n"})
    assert out["state"] == "done"


@pytest.mark.parametrize("tagged", [{"rust/pg-old/src/main.rs": "fn main() { tagged(); }\n"},
                                    {"rust/pg-old/src/main.rs": None}])
def test_a_tagged_edit_or_deletion_of_an_existing_entry_point_ships_no_new_host_binary(
        w, tmp_path, monkeypatch, tagged):
    out = _close_pr_repo(w, tmp_path, monkeypatch, {"docs/x.md": "x\n"}, tagged)
    assert out["state"] == "done"


def test_an_untagged_deletion_of_an_existing_entry_point_ships_no_new_host_binary(w, tmp_path, monkeypatch):
    out = _close_pr_repo(w, tmp_path, monkeypatch, {"rust/pg-old/src/main.rs": None})
    assert out["state"] == "done"


def test_a_tagged_merge_is_read_against_its_first_parent_only(w, tmp_path, monkeypatch):
    # The default branch gains a binary after the PR branched; the PR itself is docs only.
    out = _close_pr_repo(w, tmp_path, monkeypatch, {"docs/x.md": "x\n"},
                         main_after={"rust/pg-main/src/main.rs": "fn main() {}\n"},
                         tag_merge=True)
    assert out["state"] == "done"


def test_an_added_host_binary_in_an_untagged_follow_up_needs_installed_proof(w, tmp_path, monkeypatch):
    out = _close_pr_repo(w, tmp_path, monkeypatch,
                         {"rust/pg-new/src/main.rs": "fn main() {}\n"})
    assert out["missing"] == ["alpha:bin:pg-new"]


# ----- LE-MERGE-SUBJECT-WITHOUT-PR-NUMBER: GO row PR fallback when merge subject lacks #N

def _subjectless_repo(tmp_path, subject):
    import os
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    def git(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True, env=env).stdout.strip()
    repo = tmp_path / "subj-repo"
    repo.mkdir()
    git(repo, "-c", "init.defaultBranch=main", "init", "-q")
    git(repo, "commit", "--allow-empty", "-m", "seed")
    git(repo, "checkout", "-q", "-b", "prior")
    git(repo, "commit", "--allow-empty", "-m", "prior pr")
    prior_head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "prior (#10)", "prior")
    git(repo, "checkout", "-q", "-b", "batch7")
    git(repo, "commit", "--allow-empty", "-m", "work\n\nPlan-Item: demo:A")
    anc_head = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "--allow-empty", "-m", "batch tip")
    tip_head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", subject, "batch7")
    git(repo, "checkout", "-q", "-b", "loose")
    git(repo, "commit", "--allow-empty", "-m", "unmerged")
    unmerged = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return repo, prior_head, anc_head, tip_head, unmerged


@pytest.mark.parametrize("use_ancestor", [False, True])
def test_subjectless_merge_closes_via_go_row_pr_and_head(w, tmp_path, monkeypatch, use_ancestor):
    repo, _, anc_head, tip_head, _ = _subjectless_repo(
        tmp_path, "Spawner admission window, session counter, prepush bound (Batch 7, GO D-6395)")
    from plangraph.keeper.evidence_git import GitAdapter
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", w.real_plan_item_commits)
    monkeypatch.setattr("plangraph.keeper.context_sources.open_lanes", lambda *a, **kw: [])
    w.store.ctx["git"] = GitAdapter({"demo": {"path": str(repo), "ref": "main"}})
    w.conn.execute("UPDATE items SET state='open' WHERE key='demo:A'")
    chosen = anc_head if use_ancestor else tip_head
    w.decision_path.write_text(f"| D-6395 | demo | GO #1078 demo:A; GO-HEAD {chosen} | ROUTER |\n")
    out = w.store.close_merged(w.p("router"), "demo:A", "D-6395", ["google"],
                               from_open=True, apply=True)
    assert out["state"] == "done"
    assert w.store._evidence("demo:A")[-1]["ref"]["prs"] == [
        {"pr": 1078, "head": chosen, "decision_id": "D-6395"}]


@pytest.mark.parametrize("subject,pr", [
    ("Follow up on #42 (#1078)", 1078),
    ("Fix #7 and #8 (#9)", 9),
    ("Merge pull request #12 from x/y", 12),
    ("no number", None),
    ("see #5 and #6", 5),
])
def test_merge_subject_pr_prefers_the_trailing_canonical_number(subject, pr):
    from plangraph.keeper.context_sources import subject_pr
    assert subject_pr(subject) == pr


def test_a_title_with_an_earlier_reference_closes_on_the_trailing_pr(w, tmp_path, monkeypatch):
    repo, _, _, tip_head, _ = _subjectless_repo(tmp_path, "Follow up on #42 (#1078)")
    from plangraph.keeper.evidence_git import GitAdapter
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", w.real_plan_item_commits)
    monkeypatch.setattr("plangraph.keeper.context_sources.open_lanes", lambda *a, **kw: [])
    w.store.ctx["git"] = GitAdapter({"demo": {"path": str(repo), "ref": "main"}})
    w.conn.execute("UPDATE items SET state='open' WHERE key='demo:A'")
    w.decision_path.write_text(f"| D-6395 | demo | GO #1078 demo:A; GO-HEAD {tip_head} | ROUTER |\n")
    out = w.store.close_merged(w.p("router"), "demo:A", "D-6395", ["google"],
                               from_open=True, apply=True)
    assert out["state"] == "done"
    assert w.store._evidence("demo:A")[-1]["ref"]["prs"] == [
        {"pr": 1078, "head": tip_head, "decision_id": "D-6395"}]


def test_the_work_index_records_the_trailing_pr_of_the_merge_that_introduced_a_commit(tmp_path):
    repo, _, _, tip_head, _ = _subjectless_repo(tmp_path, "Follow up on #42 (#1078)")
    from plangraph.keeper import context_sources
    from plangraph.keeper.evidence_git import GitAdapter
    adapter = GitAdapter({"demo": {"path": str(repo), "ref": "main"}})
    index = context_sources.work_index(adapter, "demo", fresh=True)
    assert [c["merge"]["pr"] for c in index["commits"]["demo:A"]] == [1078]
    commits = context_sources.plan_item_commits(adapter, "demo", "demo:A", fetch=False)
    assert [c["merge"]["pr"] for c in commits] == [1078]
    assert context_sources.pr_merge_of(adapter, "demo", tip_head, fallback_pr=None)["pr"] == 1078


@pytest.mark.parametrize("case", ["no_pr_in_go", "prior_head", "unmerged_head", "other_pr_on_ancestor"])
def test_subjectless_merge_refuses_bad_pr_or_head(w, tmp_path, monkeypatch, case):
    subject = ("batch (#1078)" if case == "other_pr_on_ancestor"
               else "Batch 7 without pr number")
    repo, prior_head, anc_head, tip_head, unmerged = _subjectless_repo(tmp_path, subject)
    from plangraph.keeper.evidence_git import GitAdapter
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", w.real_plan_item_commits)
    monkeypatch.setattr("plangraph.keeper.context_sources.open_lanes", lambda *a, **kw: [])
    w.store.ctx["git"] = GitAdapter({"demo": {"path": str(repo), "ref": "main"}})
    w.conn.execute("UPDATE items SET state='open' WHERE key='demo:A'")
    row = {
        "no_pr_in_go": f"GO {tip_head}",
        "prior_head": f"GO #1078 demo:A; GO-HEAD {prior_head}",
        "unmerged_head": f"GO #1078 demo:A; GO-HEAD {unmerged}",
        "other_pr_on_ancestor": f"GO #999 demo:A; GO-HEAD {anc_head}",
    }[case]
    w.decision_path.write_text(f"| D-6395 | demo | {row} | ROUTER |\n")
    with pytest.raises(Refused):
        w.store.close_merged(w.p("router"), "demo:A", "D-6395", ["google"],
                             from_open=True, apply=True)
    assert w.store._item("demo:A")["state"] == "open"


def _passed_payload(world, key):
    row = world.conn.execute("SELECT payload FROM events WHERE key=? AND type='review_passed' "
                             "ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
    return json.loads(row[0])


def test_off_closes_a_merge_without_seat_runs_and_flags_it(unseated_w):
    """Train 1 case: merge with GO row and Plan-Item, but no Review-Runs: closes, flag seats_missing."""
    w = unseated_w
    w.store.require_seats = False
    result = w.store.close_merged(w.p("router"), "demo:H", "D-5504")
    assert result["state"] == "done"
    flag = result["flags"]["seats_missing"]
    assert flag["run_ids"] == [] and "NO-WITNESS" in flag["reason"]
    assert _passed_payload(w, "demo:H")["flags"] == result["flags"]
    assert w.store._evidence("demo:H")[-1]["ref"]["flags"] == result["flags"]
    assert w.store.verify_integrity() == []


def test_off_flags_cited_run_ids_of_an_unwitnessed_review(unseated_w, tmp_path):
    w = unseated_w
    root = mirrored_reviews(w, tmp_path)
    path = root / "fixture/run-two/record.json"
    record = json.loads(path.read_text())
    record["stdout_tail"] = "TEST_GAMING: yes\nVERDICT: PASS"
    path.write_text(json.dumps(record))
    w.store.require_seats = False
    result = w.store.close_merged(w.p("router"), "demo:H", "D-5504")
    assert result["state"] == "done"
    assert result["flags"]["seats_missing"]["run_ids"] == ["run-one", "run-two"]


def test_on_still_refuses_the_same_merge_without_seats(unseated_w):
    w = unseated_w
    w.store.require_seats = True
    with pytest.raises(Refused, match="NO-WITNESS"):
        w.store.close_merged(w.p("router"), "demo:H", "D-5504")
    assert w.store._item("demo:H")["state"] == "in_review"


def test_witnessed_seats_close_without_a_flag_when_off(unseated_w, tmp_path):
    w = unseated_w
    mirrored_reviews(w, tmp_path)
    w.store.require_seats = False
    result = w.store.close_merged(w.p("router"), "demo:H", "D-5504")
    assert result["state"] == "done" and "flags" not in result


@pytest.mark.parametrize("risk", ["auth", "secrets"])
def test_off_never_flags_away_the_credential_lane_seat_rule(unseated_w, risk):
    w = unseated_w
    w.store.require_seats = False
    w.store.put_card(w.p("router"), "demo:H", {"outcome": "fixture", "risk_tags": [risk]})
    with pytest.raises(Refused, match="NO-WITNESS|insufficient_non_anthropic"):
        w.store.close_merged(w.p("router"), "demo:H", "D-5504")
    assert w.store._item("demo:H")["state"] == "in_review"
