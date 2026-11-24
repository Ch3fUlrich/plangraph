"""The deploy gate reads the same merged work on every close path (LE-MERGED-UNIT-NEVER-DEPLOYED)."""
import os
import sys

import pytest

from plangraph.keeper import context_sources
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.evidence_git import GitAdapter
from test_keeper_close_merged_scan import HEADER, _git, merged_repo
from test_keeper_merge_close_auto import auto, env, git  # noqa: F401 - env is a fixture
from test_keeper_close_merged import unseated_w, w  # noqa: F401 - the manual-close world fixtures


def _land_with_unit(root, name="feat", pr=41, tagged=True):
    """A PR whose tagged commit edits a note and whose untagged follow-up adds a unit."""
    git(root, "checkout", "-q", "-b", name, "main")
    (root / "NOTES.md").write_text("work\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "work" + ("\n\nPlan-Item: demo:A" if tagged else ""))
    (root / "deploy/systemd").mkdir(parents=True)
    (root / "deploy/systemd/new.service").write_text("[Service]\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "follow-up without a trailer")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "--no-ff", "-q", "-m", f"Merge pull request #{pr} from x/{name}", name)


def test_the_automatic_close_waits_for_deploy_proof_in_preview_and_apply(env):  # noqa: F811
    _land_with_unit(env.repo.root)
    preview = auto(env, require_seats=False, apply=False)
    assert preview["would_close"] == [] and preview["closed"] == []
    assert [r["key"] for r in preview["listed"]] == ["demo:A"]
    assert "deploy_pending" in preview["listed"][0]["reason"]
    applied = auto(env, require_seats=False)
    assert applied["closed"] == []
    assert "deploy_pending" in applied["listed"][0]["reason"]
    assert env.store._item("demo:A")["state"] == "open"
    codes = [f["code"] for f in env.store.alerts(env.p("reader"))["findings"] if f["key"] == "demo:A"]
    assert codes == ["deploy_pending"]


def test_the_automatic_close_still_closes_a_merge_that_ships_no_unit(env):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:A", 42)
    assert [c["key"] for c in auto(env, require_seats=False)["closed"]] == ["demo:A"]


def test_the_manual_close_counts_the_merge_a_merge_repo_note_names(w, monkeypatch, tmp_path):  # noqa: F811
    repo = tmp_path / "other-repo"
    merged_repo(repo, [(165, None, "2026-11-01T12:00:00Z")])
    _land_with_unit(repo, name="untagged", pr=166, tagged=False)
    landed = _git(repo, "rev-parse", "main")
    empty = tmp_path / "demo-repo"
    heads = merged_repo(empty, [])
    w.store.ctx["git"] = GitAdapter({"demo": {"path": str(empty), "ref": "main"},
                                     "other": {"path": str(repo), "ref": "main"}})
    w.store.verifiers["deploy"] = __import__("plangraph.keeper.evidence", fromlist=["VERIFIERS"]).VERIFIERS["deploy"]
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", w.real_plan_item_commits)
    head = _git(repo, "rev-parse", "main^2")
    w.decision_path.write_text(
        HEADER + f"| D-5504 | demo | GO #166 demo:A; GO-HEAD {head} | ROUTER | 2026-11-05 |\n")
    w.store.edit_definition(w.p("router"), "demo:A", {"note": f"Merge-Repo: other {landed}"})
    out = w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])
    assert out["closed"] is False and out["reason"] == "deploy_pending"
    assert w.store._item("demo:A")["state"] == "in_review"


@pytest.mark.parametrize("subject,pr", [("batch l1: #1 #2 #3 (#100)", 100), ("Fix #7 (#9)", 9)])
def test_both_merge_subject_paths_read_the_trailing_pr(tmp_path, subject, pr):
    """A merge that carries the trailers itself, and a merge named by its sha, give the PR of `(#N)`."""
    repo = tmp_path / "repo"
    merged_repo(repo, [])
    git(repo, "checkout", "-q", "-b", "feat", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "work")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "-m", f"{subject}\n\nPlan-Item: demo:A", "feat")
    merge = git(repo, "rev-parse", "HEAD")
    adapter = GitAdapter({"demo": {"path": str(repo), "ref": "main"}})
    r, ref = adapter.repo_path("demo"), adapter.repo_ref("demo")
    assert context_sources._merge_of(adapter, r, ref, merge)["pr"] == pr
    assert context_sources.named_merge(adapter, "demo", merge)["pr"] == pr


def frozen(env, **kw):  # noqa: F811
    """What the API does: warm the scan outside the lock, then run the verb frozen."""
    env.store.warm_merge_scan(env.store.merge_candidates())
    return env.store.close_merged_auto(env.p("system"), frozen=True, **kw)


def test_the_frozen_automatic_close_resolves_no_named_merge_under_its_lock(env, monkeypatch):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:A", 43)
    merge = git(env.repo.root, "rev-parse", "HEAD")
    env.conn.execute("UPDATE items SET description=? WHERE key='demo:A'", (f"Merge-Repo: demo {merge}",))
    env.store.warm_merge_scan(env.store.merge_candidates())  # outside the lock: may fetch and resolve
    def forbidden(*args, **kwargs):
        pytest.fail("frozen close must consume the warmed named merge and deploy diffs")
    monkeypatch.setattr(env.store, "_named_merge_proof", forbidden)
    monkeypatch.setattr(env.store.ctx["git"], "deploy_changes", forbidden)
    result = env.store.close_merged_auto(env.p("system"), require_seats=False, frozen=True)
    assert [c["key"] for c in result["closed"]] == ["demo:A"]


def test_a_frozen_close_holds_an_item_whose_separately_named_merge_ships_a_unit(env):  # noqa: F811
    """A tagged docs merge, plus a `Merge-Repo:` SHA naming another merge that adds a unit."""
    env.repo.land("docs", "Plan-Item: demo:A", 45)
    root = env.repo.root
    git(root, "checkout", "-q", "-b", "unit", "main")
    (root / "deploy/systemd").mkdir(parents=True)
    (root / "deploy/systemd/new.service").write_text("[Service]\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "unit without a trailer")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "--no-ff", "-q", "-m", "Merge pull request #46 from x/unit", "unit")
    named = git(root, "rev-parse", "HEAD")
    env.conn.execute("UPDATE items SET description=? WHERE key='demo:A'", (f"Merge-Repo: demo {named}",))
    for run in (lambda: frozen(env, require_seats=False),
                lambda: auto(env, require_seats=False)):
        result = run()
        assert result["closed"] == []
        assert "deploy_pending" in result["listed"][0]["reason"]
    assert env.store._item("demo:A")["state"] == "open"


def test_a_frozen_close_without_a_warm_deploy_scan_closes_nothing(env):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:A", 47)
    env.store.warm_merge_scan()  # no candidates: the indexes are warm, the deploy scan is not
    result = env.store.close_merged_auto(env.p("system"), require_seats=False, frozen=True)
    assert result["closed"] == [] and "deploy_scan_not_warm" in result["listed"][0]["reason"]


def test_a_failed_history_read_is_not_an_empty_deploy_scan(env, monkeypatch):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:A", 48)
    real = context_sources.plan_item_commits

    def history(git_, project, key, **kw):
        # Only the deploy scan's own read fails; the close's proof of the merge still sees the commits.
        scan = sys._getframe(1).f_code.co_name == "_deploy_changes"
        return None if scan else real(git_, project, key, **kw)

    monkeypatch.setattr(context_sources, "plan_item_commits", history)
    for run in (lambda: auto(env, require_seats=False),
                lambda: frozen(env, require_seats=False)):  # frozen: the snapshot read the failure
        result = run()
        assert result["closed"] == []
        assert "deploy_scan_unreadable" in result["listed"][0]["reason"]
    assert env.store._item("demo:A")["state"] == "open"


def test_an_unreadable_deploy_scan_leaves_the_item_open_on_the_automatic_close(env, monkeypatch):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:A", 44)

    def broken(*args, **kwargs):
        raise RuntimeError("cannot read commit changes")

    monkeypatch.setattr(env.store.ctx["git"], "deploy_changes", broken)
    for apply in (False, True):
        result = auto(env, require_seats=False, apply=apply)
        assert result["closed"] == [] and result["would_close"] == []
        assert "deploy_scan_unreadable" in result["listed"][0]["reason"]
    assert env.store._item("demo:A")["state"] == "open"


def test_a_squash_merged_hosted_pr_is_scanned_at_its_landed_commit_not_its_topic_head(w, monkeypatch, tmp_path):  # noqa: F811
    import json
    import subprocess
    repo = tmp_path / "other"
    merged_repo(repo, [])
    # The PR's topic head never reaches main; main gets one squash commit that adds the unit.
    _git(repo, "checkout", "-q", "-b", "topic")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "topic work")
    topic = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    (repo / "deploy/systemd").mkdir(parents=True)
    (repo / "deploy/systemd/new.service").write_text("[Service]\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature (#166)")
    squash = _git(repo, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "origin", "https://github.com/example/other")
    empty = tmp_path / "demo"
    merged_repo(empty, [])
    w.store.ctx["git"] = GitAdapter({"demo": {"path": str(empty), "ref": "main"},
                                     "other": {"path": str(repo), "ref": "main"}})
    w.store.verifiers["deploy"] = __import__("plangraph.keeper.evidence", fromlist=["VERIFIERS"]).VERIFIERS["deploy"]
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", w.real_plan_item_commits)
    w.decision_path.write_text(
        HEADER + f"| D-5504 | demo | GO other PR #166 demo:A; GO-HEAD {topic} | ROUTER | 2026-11-05 |\n")
    run = subprocess.run

    def hosted(args, **kwargs):
        if args[0] != "gh":
            return run(args, **kwargs)
        info = {"state": "MERGED", "mergeCommit": {"oid": squash}, "headRefOid": topic}
        return subprocess.CompletedProcess(args, 0, json.dumps(info))

    monkeypatch.setattr(subprocess, "run", hosted)
    router = w.p("router")
    w.store.edit_definition(router, "demo:A", {"note": "Merge-Repo: other #166"})
    out = w.store.close_merged(router, "demo:A", "D-5504", ["google"])
    assert out["closed"] is False and out["reason"] == "deploy_pending"
    assert out["missing"] == ["(hosts unnamed)"]


def test_a_snapshot_older_than_the_work_the_close_proves_is_stale(env):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:A", 50)
    env.store.warm_merge_scan(env.store.merge_candidates())
    # After the warm-up a second PR of the same item lands and ships a unit.
    root = env.repo.root
    git(root, "checkout", "-q", "-b", "later", "main")
    (root / "deploy/systemd").mkdir(parents=True)
    (root / "deploy/systemd/late.service").write_text("[Service]\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "late unit\n\nPlan-Item: demo:A")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "--no-ff", "-q", "-m", "Merge pull request #51 from x/later", "later")
    result = env.store.close_merged_auto(env.p("system"), require_seats=False, frozen=True)
    assert result["closed"] == [] and "merge_index_tip_changed" in result["listed"][0]["reason"]
    assert env.store._item("demo:A")["state"] == "open"
    # A fresh warm-up sees both PRs and finds the unit.
    again = frozen(env, require_seats=False)
    assert again["closed"] == [] and "deploy_pending" in again["listed"][0]["reason"]


def test_a_failed_merge_lookup_is_an_unreadable_scan_not_the_commits_own_diff(env, monkeypatch):  # noqa: F811
    _land_with_unit(env.repo.root, pr=52)
    monkeypatch.setattr(GitAdapter, "merge_commit_of", lambda *a, **k: None)  # as after a git failure
    for run in (lambda: auto(env, require_seats=False), lambda: frozen(env, require_seats=False)):
        result = run()
        assert result["closed"] == []
        assert "deploy_scan_unreadable" in result["listed"][0]["reason"]
    assert env.store._item("demo:A")["state"] == "open"


def test_a_direct_commit_on_the_ref_has_no_merge_to_find(tmp_path):
    repo = tmp_path / "repo"
    merged_repo(repo, [])
    (repo / "deploy/systemd").mkdir(parents=True)
    (repo / "deploy/systemd/direct.service").write_text("[Service]\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "direct unit\n\nPlan-Item: demo:A")
    sha = _git(repo, "rev-parse", "HEAD")
    adapter = GitAdapter({"demo": {"path": str(repo), "ref": "main"}})
    r, ref = adapter.repo_path("demo"), adapter.repo_ref("demo")
    assert ("A", "deploy/systemd/direct.service") in adapter.deploy_changes(r, sha, ref)
    # An older direct commit, with newer work after it, is still on the chain.
    _git(repo, "commit", "-q", "--allow-empty", "-m", "later")
    assert ("A", "deploy/systemd/direct.service") in adapter.deploy_changes(r, sha, ref)


def test_a_snapshot_of_a_rebuilt_ref_is_stale_even_with_the_same_tagged_commit(env):  # noqa: F811
    tagged = env.repo.land("docs", "Plan-Item: demo:A", 53)
    env.store.warm_merge_scan(env.store.merge_candidates())
    # The default ref is rebuilt: the same tagged commit comes in through a different merge that
    # also brings an untagged unit commit.
    root = env.repo.root
    git(root, "reset", "-q", "--hard", "HEAD^")
    git(root, "checkout", "-q", "-b", "redo", tagged)
    (root / "deploy/systemd").mkdir(parents=True)
    (root / "deploy/systemd/redo.service").write_text("[Service]\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "unit without a trailer")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "--no-ff", "-q", "-m", "Merge pull request #54 from x/redo", "redo")
    result = env.store.close_merged_auto(env.p("system"), require_seats=False, frozen=True)
    assert result["closed"] == [] and "merge_index_tip_changed" in result["listed"][0]["reason"]
    assert env.store._item("demo:A")["state"] == "open"
    again = frozen(env, require_seats=False)
    assert again["closed"] == [] and "deploy_pending" in again["listed"][0]["reason"]


def test_a_ref_that_moves_away_and_back_during_the_scan_cannot_hide_the_unit(env, monkeypatch):  # noqa: F811
    _land_with_unit(env.repo.root, pr=55)
    root = env.repo.root
    final = git(root, "rev-parse", "HEAD")
    tagged = git(root, "rev-parse", "main^2~1")  # the tagged commit, before the untagged follow-up
    # An alternative history: the same tagged commit merged without the follow-up.
    git(root, "checkout", "-q", "-b", "alt", "main~1")
    git(root, "merge", "--no-ff", "-q", "-m", "Merge pull request #55 from x/feat", tagged)
    alt = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "main")
    git(root, "branch", "-q", "-D", "alt")  # a lane would hold the close; the commit stays for update-ref
    real = GitAdapter.deploy_changes

    def excursion(self, repo, sha, ref, item_key=None):
        git(root, "update-ref", "refs/heads/main", alt)  # the ref is elsewhere while the diff is read
        try:
            return real(self, repo, sha, ref, item_key)
        finally:
            git(root, "update-ref", "refs/heads/main", final)

    monkeypatch.setattr(GitAdapter, "deploy_changes", excursion)
    result = frozen(env, require_seats=False)
    assert result["closed"] == [] and "deploy_pending" in result["listed"][0]["reason"]


def test_a_ref_that_moves_between_the_check_and_the_write_closes_nothing(env, monkeypatch):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:A", 56)
    root = env.repo.root
    parents = git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()
    # The same PR and topic head through a replacement merge: the proofs (pr, head) are equal.
    replacement = git(root, "commit-tree", f"{parents[0]}^{{tree}}", "-p", parents[1], "-p", parents[2],
                      "-m", "Merge pull request #56 from x/docs (replacement)")
    real = type(env.store)._write

    def moved(store):
        git(root, "update-ref", "refs/heads/main", replacement)
        return real(store)

    monkeypatch.setattr(type(env.store), "_write", moved)
    result = auto(env, require_seats=False)
    assert result["closed"] == []
    assert "merged_on_default_changed" in result["listed"][0]["reason"]


def test_replacement_during_the_final_ref_read_cannot_close(env, monkeypatch):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:A", 57)
    root = env.repo.root
    original, parent, topic = git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()
    git(root, "checkout", "-q", "--detach", original)
    (root / "deploy/systemd").mkdir(parents=True)
    (root / "deploy/systemd/replacement.service").write_text("[Service]\n")
    git(root, "add", "-A")
    tree = git(root, "write-tree")
    replacement = git(root, "commit-tree", tree, "-p", parent, "-p", topic,
                      "-m", "Merge pull request #57 from x/docs (replacement)")
    git(root, "checkout", "-q", "-f", "main")
    env.store.warm_merge_scan(env.store.merge_candidates())
    adapter = env.store.ctx["git"]
    ref_commit, moved = adapter.ref_commit, []
    def move_after_read(repo, ref):
        sha = ref_commit(repo, ref)
        if ref == "main" and env.conn.in_transaction and not moved:
            git(root, "update-ref", "refs/heads/main", replacement, original)
            moved.append(sha)
        return sha

    monkeypatch.setattr(adapter, "ref_commit", move_after_read)
    result = env.store.close_merged_auto(env.p("system"), require_seats=False, frozen=True)
    assert moved == [original]
    assert result["closed"] == []
    assert "merged_on_default_changed" in result["listed"][0]["reason"]
    assert env.store._item("demo:A")["state"] == "open"


def test_automatic_close_pins_tagged_named_and_deploy_reads(env, monkeypatch):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:A", 59)
    root = env.repo.root
    original = git(root, "rev-parse", "HEAD")
    env.conn.execute("UPDATE items SET description=? WHERE key='demo:A'", (f"Merge-Repo: demo {original}",))
    adapter = env.store.ctx["git"]
    run = adapter._run
    revisions = []

    def record(repo, args, **kw):
        if args[0] == "log" and "--grep" in args:
            revisions.append(("tagged", args[-1]))
        if args[:2] == ["merge-base", "--is-ancestor"] and args[-2] == original and not args[-1].endswith("^1"):
            revisions.append(("named", args[-1]))
        return run(repo, args, **kw)

    monkeypatch.setattr(adapter, "_run", record)
    scan = adapter.deploy_changes

    def record_scan(repo, sha, ref, item_key=None):
        revisions.append(("deploy", ref))
        return scan(repo, sha, ref, item_key)

    monkeypatch.setattr(adapter, "deploy_changes", record_scan)
    result = frozen(env, require_seats=False)
    assert [row["key"] for row in result["closed"]] == ["demo:A"]
    assert {kind for kind, _ in revisions} == {"tagged", "named", "deploy"}
    assert {ref for _, ref in revisions} == {original}


@pytest.mark.parametrize("related", [False, True])
def test_manual_close_handles_a_fast_forward_after_its_diff(w, monkeypatch, tmp_path, related):  # noqa: F811
    root = tmp_path / "repo"
    merged_repo(root, [])
    _land_with_unit(root, pr=58)
    original = git(root, "rev-parse", "HEAD")
    topic = git(root, "rev-parse", "HEAD^2")
    w.decision_path.write_text(
        HEADER + f"| D-5504 | demo | GO #58 demo:A; GO-HEAD {topic} | ROUTER | 2026-11-05 |\n")
    adapter = GitAdapter({"demo": {"path": str(root), "ref": "main"}})
    w.store.ctx["git"] = adapter
    monkeypatch.setattr(context_sources, "plan_item_commits", w.real_plan_item_commits)
    scan = adapter.deploy_changes
    scanned = []

    def fast_forward(repo, sha, ref, item_key=None):
        changes = scan(repo, sha, ref, item_key)
        if not scanned:
            message = "more work\n\nPlan-Item: demo:A" if related else "unrelated work"
            git(root, "commit", "-q", "--allow-empty", "-m", message)
        scanned.append(ref)
        return changes

    monkeypatch.setattr(adapter, "deploy_changes", fast_forward)
    result = w.store.close_merged(w.p("router"), "demo:A", "D-5504", ["google"])
    assert scanned[0] == original
    assert set(scanned) == ({original, git(root, "rev-parse", "main")} if related else {original})
    assert result["closed"] is False and result["reason"] == "deploy_pending"
    assert w.store._item("demo:A")["state"] == "in_review"


@pytest.mark.parametrize("subject", ["Landing train 9 (#57)", "batch train-9: #55 #56 (#57)"])
def test_train_deploy_gate_attributes_units_to_their_part(env, subject):  # noqa: F811
    root = env.repo.root
    base = git(root, "rev-parse", "main")
    git(root, "checkout", "-q", "-b", "train")
    for branch, item in (("docs", "demo:A"), ("unit", "demo:B")):
        git(root, "checkout", "-q", "-b", branch, base)
        git(root, "commit", "-q", "--allow-empty", "-m", f"work\n\nPlan-Item: {item}")
        if branch == "unit":
            (root / "deploy/systemd").mkdir(parents=True)
            (root / "deploy/systemd/train.service").write_text("[Service]\n")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "unit follow-up")
        git(root, "checkout", "-q", "train")
        git(root, "merge", "-q", "--no-ff", "-m", f"Train 9: merge {branch}", branch)
        git(root, "branch", "-D", branch)
    (root / "deploy/systemd/resolution.socket").write_text("[Socket]\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "train resolution\n\nPlan-Item: demo:BATCH")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "-q", "--no-ff", "-m",
        f"{subject}\n\nPlan-Item: demo:A\nPlan-Item: demo:B\nPlan-Item: demo:BATCH\nPlan-Item: demo:C", "train")
    git(root, "branch", "-D", "train")

    store = env.store
    docs = store._pin_close(store._item("demo:A"))
    unit = dict(docs, key="demo:B")
    assert store._deploy_changes(docs)[1] == set()
    assert store._deploy_changes(unit)[1] == {("A", "deploy/systemd/train.service")}
    assert store._deploy_changes(dict(docs, key="demo:BATCH"))[1] == {
        ("A", "deploy/systemd/resolution.socket"),
    }
    assert store._deploy_changes(dict(docs, key="demo:C"))[1] == set()
    assert [c["key"] for c in auto(env, require_seats=False)["closed"]] == ["demo:A", "demo:C"]


def test_train_deploy_hosts_are_read_from_the_part_that_ships_the_unit(env):  # noqa: F811
    root = env.repo.root
    base = git(root, "rev-parse", "main")
    git(root, "checkout", "-q", "-b", "train")
    for branch, key, hosts in (("notes", "demo:A", "wrong-host"), ("unit", "demo:B", "alpha, beta")):
        git(root, "checkout", "-q", "-b", branch, base)
        if branch == "unit":
            (root / "deploy/systemd").mkdir(parents=True)
            (root / "deploy/systemd/train.service").write_text("[Service]\n")
            git(root, "add", "-A")
        git(root, "commit", "-q", "--allow-empty", "-m", f"work\n\nPlan-Item: {key}")
        git(root, "checkout", "-q", "train")
        git(root, "merge", "-q", "--no-ff", "-m", f"Train 9: merge {branch}\n\nDeploy: {hosts}", branch)
        git(root, "branch", "-D", branch)
    git(root, "checkout", "-q", "main")
    git(root, "merge", "-q", "--no-ff", "-m", "Landing train 9 (#57)", "train")
    git(root, "branch", "-D", "train")

    result = auto(env, require_seats=False)
    assert [c["key"] for c in result["closed"]] == ["demo:A"]
    reason = next(r["reason"] for r in result["listed"] if r["key"] == "demo:B")
    assert "alpha:train.service" in reason and "beta:train.service" in reason
    assert "wrong-host" not in reason and "hosts unnamed" not in reason

    from plangraph.keeper.evidence import VERIFIERS
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    for host in ("alpha", "beta"):
        env.store.add_evidence_router(env.p("router"), "demo:B", "deploy",
                                      {"host": host, "unit": "train.service", "is_active": "active"})
    assert [c["key"] for c in auto(env, require_seats=False)["closed"]] == ["demo:B"]


@pytest.mark.parametrize("subject", ["Landing train 9: 1 batch (#57)", "Old batch delivery (#57)"])
def test_train_deploy_gate_keeps_other_keys_units_out_of_a_shared_part(env, subject):  # noqa: F811
    """Two keys share one part: only the key whose own commit adds a unit, or the untagged
    follow-up of the part, is deploy-pending (LE-DEPLOY-ATTRIBUTION-OLD-TRAINS-BLOCKS-UNITLESS-KEYS)."""
    root = env.repo.root
    base = git(root, "rev-parse", "main")
    git(root, "checkout", "-q", "-b", "train")
    git(root, "checkout", "-q", "-b", "shared", base)
    (root / "NOTES.md").write_text("docs\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "docs\n\nPlan-Item: demo:A")
    (root / "deploy/systemd").mkdir(parents=True)
    (root / "deploy/systemd/shared.service").write_text("[Service]\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "unit\n\nPlan-Item: demo:B")
    git(root, "checkout", "-q", "train")
    git(root, "merge", "-q", "--no-ff", "-m", "Train 9: merge shared", "shared")
    git(root, "branch", "-D", "shared")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "-q", "--no-ff", "-m", subject, "train")
    git(root, "branch", "-D", "train")

    store = env.store
    docs = store._pin_close(store._item("demo:A"))
    assert store._deploy_changes(docs)[1] == {("A", "NOTES.md")}
    assert store._deploy_changes(dict(docs, key="demo:B"))[1] == {("A", "deploy/systemd/shared.service")}
    result = auto(env, require_seats=False)
    assert [c["key"] for c in result["closed"]] == ["demo:A"]
    assert "deploy_pending" in next(r["reason"] for r in result["listed"] if r["key"] == "demo:B")


@pytest.mark.parametrize("new_target,resolution,cross_repo", [
    ("deploy/systemd/new.service", False, False),
    ("rust/demo/src/main.rs", False, False),
    ("deploy/systemd/new.service", True, False),
    ("rust/demo/src/main.rs", True, False),
    ("deploy/systemd/new.service", False, True),
])
def test_direct_pr_deploy_gate_ignores_main_sync_and_other_keys(
        env, monkeypatch, tmp_path, new_target, resolution, cross_repo):
    root = env.repo.root
    git(root, "checkout", "-qb", "topic")
    git(root, "commit", "--allow-empty", "-qm", "notes\n\nPlan-Item: demo:A")
    git(root, "checkout", "-q", "main")
    unit_dir = root / "deploy/systemd"
    unit_dir.mkdir(parents=True)
    (unit_dir / "already.service").write_text("[Service]\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "unrelated main delivery")
    git(root, "checkout", "-q", "topic")
    git(root, "merge", "--no-ff", "--no-commit", "-q", "main")
    target = root / new_target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("fixture\n")
    if not resolution:
        git(root, "commit", "-qm", "sync main\n\nPlan-Item: demo:A")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "unit\n\nPlan-Item: demo:B")
    head = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "--no-ff", "-qm", "Merge pull request #57 from x/topic", "topic")
    git(root, "branch", "-D", "topic")
    decision_path = tmp_path / "decisions.md"
    decision_path.write_text(
        HEADER + f"| D-5504 | demo | GO #57 demo:A; GO-HEAD {head} | ROUTER | 2026-11-05 |\n")
    env.store.ctx["decisions"] = DecisionsAdapter(decision_path)
    monkeypatch.setattr(context_sources, "open_lanes", lambda *args, **kwargs: [])
    if cross_repo:
        empty = tmp_path / "primary"
        merged_repo(empty, [])
        env.store.ctx["git"] = GitAdapter({"demo": str(empty), "other": str(root)})
        env.store.edit_definition(env.p("router"), "demo:A", {"note": "Merge-Repo: other"})

    preview = env.store.close_merged(env.p("router"), "demo:A", "D-5504", ["google"],
                                     from_open=True, require_seats=False)
    assert preview.get("would_close") is True, preview
    if not cross_repo:
        result = auto(env, require_seats=False, apply=False)
        assert [row["key"] for row in result["would_close"]] == ["demo:A"]
        assert "deploy_pending" in next(row["reason"] for row in result["listed"] if row["key"] == "demo:B")


def _train_with_part(env, build, *, evil=None):
    """A train of one part: *build* makes the part's commits, *evil* edits the part merge itself."""
    root = env.repo.root
    base = git(root, "rev-parse", "main")
    git(root, "checkout", "-q", "-b", "train")
    git(root, "checkout", "-q", "-b", "part", base)
    build(root)
    git(root, "checkout", "-q", "train")
    if evil:
        git(root, "merge", "-q", "--no-ff", "--no-commit", "part")
        evil(root)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "Train 9: merge part")
    else:
        git(root, "merge", "-q", "--no-ff", "-m", "Train 9: merge part", "part")
    git(root, "branch", "-D", "part")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "-q", "--no-ff", "-m", "Landing train 9: 1 batch (#57)", "train")
    git(root, "branch", "-D", "train")
    store = env.store
    return store._pin_close(store._item("demo:A"))


def test_train_part_net_change_ignores_a_unit_added_then_removed(env):  # noqa: F811
    def build(root):
        (root / "deploy/systemd").mkdir(parents=True)
        (root / "deploy/systemd/temp.service").write_text("[Service]\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "scratch unit\n\nPlan-Item: demo:A")
        git(root, "rm", "-q", "deploy/systemd/temp.service")
        git(root, "commit", "-q", "-m", "remove it\n\nPlan-Item: demo:A")

    pinned = _train_with_part(env, build)
    assert env.store._deploy_changes(pinned)[1] == set()


def test_train_part_merge_commit_own_unit_counts_for_the_part_key(env):  # noqa: F811
    def build(root):
        (root / "NOTES.md").write_text("docs\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "docs\n\nPlan-Item: demo:A")

    def evil(root):
        (root / "deploy/systemd").mkdir(parents=True)
        (root / "deploy/systemd/resolved.service").write_text("[Service]\n")

    pinned = _train_with_part(env, build, evil=evil)
    assert ("A", "deploy/systemd/resolved.service") in env.store._deploy_changes(pinned)[1]


@pytest.mark.parametrize("env", ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not os.environ.get("PG_DSN"), reason="PG_DSN not set"))], indirect=True)
def test_merge_close_reports_the_age_of_unproven_unit_and_live_work(env):
    from plangraph.keeper.evidence_types import as_epoch

    env.store.edit_definition(env.p("router"), "demo:A", {"note": "Deploy-Hosts: fixture"})
    env.store.edit_definition(env.p("router"), "demo:B", {
        "done_criteria": "Live: merge-close finishes under 120 seconds.",
        "criteria_reason": "Require live timing evidence",
        "criteria_evidence": "Merge-close timing fixture",
    })
    _land_with_unit(env.repo.root, name="timer")
    env.repo.land("live-clause", "Plan-Item: demo:B", 97)
    env.clock.t = as_epoch(git(env.repo.root, "show", "-s", "--format=%cI", "main")) + 26 * 3600
    preview = frozen(env, require_seats=False, apply=False)
    pending = {row["key"]: row for row in preview["deployment_pending"]}
    assert pending["demo:A"]["missing"] == ["fixture:new.service"]
    assert pending["demo:B"]["missing"] == ["live_evidence_missing"]
    assert all(row["age_hours"] >= 26 and row["merged_at"] for row in pending.values())
    assert preview["merged_not_done"] == 2
    assert [row["key"] for row in preview["would_close"]] == ["demo:B"]
    assert all(env.store._item(key)["state"] == "open" for key in pending)
    applied = frozen(env, require_seats=False)
    assert [row["key"] for row in applied["closed"]] == ["demo:B"]
    assert [row["key"] for row in applied["listed"]] == ["demo:A"]
    assert applied["deployment_pending"] == preview["deployment_pending"]
    assert applied["merged_not_done"] == 1
    assert env.store._item("demo:B")["state"] == "done"
    assert [(row["key"], row["code"]) for row in env.store.alerts(env.p("reader"))["findings"]
            if row["code"] == "deploy_pending"] == [("demo:A", "deploy_pending")]


def test_a_live_clause_requires_verified_evidence_from_after_the_merge(env):
    from plangraph.keeper.evidence import VERIFIERS
    from plangraph.keeper.evidence_types import as_epoch

    key = "demo:A"
    env.store.edit_definition(env.p("router"), key, {
        "done_criteria": "Live: observe the service in use.",
        "criteria_reason": "Require a live service observation",
        "criteria_evidence": "Service deployment fixture",
    })
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    env.repo.land("live", f"Plan-Item: {key}", 98)
    merged = as_epoch(git(env.repo.root, "show", "-s", "--format=%cI", "main"))
    env.clock.t = merged - 60
    proof = {"host": "fixture", "unit": "test.service", "is_active": "active"}
    assert env.store.add_evidence_router(env.p("router"), key, "deploy", proof)["verified"]
    env.clock.t = merged + 60
    preview = frozen(env, require_seats=False, apply=False)
    assert preview["deployment_pending"][0]["key"] == key
    assert preview["would_close"][0]["key"] == key
    assert env.store.add_evidence_router(env.p("router"), key, "deploy", proof)["verified"]
    result = frozen(env, require_seats=False, apply=False)
    assert result["deployment_pending"] == [] and result["would_close"][0]["key"] == key


# --- TRAIN-DEPLOYMENT-EVIDENCE-GO-ENFORCEMENT: the inventory also covers completed items ---


def _done(env, key, **columns):
    sets = ", ".join(["state='done'"] + [f"{name}=?" for name in columns])
    env.store.conn.execute(f"UPDATE items SET {sets} WHERE key=?", (*columns.values(), key))


def _inventory(env, **kw):
    env.store.include_done_in_scan = True
    env.store.warm_merge_scan(env.store.merge_candidates())
    return env.store.close_merged_auto(env.p("system"), require_seats=False, apply=False, frozen=True, **kw)


def test_a_done_item_that_shipped_a_unit_without_deploy_proof_is_listed(env):  # noqa: F811
    _land_with_unit(env.repo.root)
    _done(env, "demo:A", note="Deploy-Hosts: host-a")
    report = _inventory(env, inventory=True)
    rows = {row["key"]: row for row in report["deployment_pending"]}
    assert rows["demo:A"]["missing"] == ["host-a:new.service"]
    assert rows["demo:A"]["state"] == "done" and rows["demo:A"]["age_hours"] is not None
    assert report["closed"] == [] and report["would_close"] == [] and report["listed"] == []
    assert env.store._item("demo:A")["state"] == "done"


def test_a_done_item_with_a_live_clause_and_no_live_proof_is_listed(env):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:B", 42)
    _done(env, "demo:B", done_criteria="Tests pass. Live: the service answers on host-a")
    rows = {row["key"]: row for row in _inventory(env, inventory=True)["deployment_pending"]}
    assert rows["demo:B"]["missing"] == ["live_evidence_missing"]


def test_a_done_item_without_units_or_live_clause_is_not_listed(env):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:C", 43)
    _done(env, "demo:C")
    assert _inventory(env, inventory=True)["deployment_pending"] == []


def test_the_ordinary_close_never_looks_at_done_items(env):  # noqa: F811
    _land_with_unit(env.repo.root)
    _done(env, "demo:A")
    report = _inventory(env)
    assert report["deployment_pending"] == [] and report["listed"] == []


def test_the_scan_leaves_done_items_out_unless_the_keeper_asks_for_them(env):  # noqa: F811
    _land_with_unit(env.repo.root)
    _done(env, "demo:A")
    assert "demo:A" not in {key for key, _ in env.store.merge_candidates()}
    env.store.include_done_in_scan = True
    assert "demo:A" in {key for key, _ in env.store.merge_candidates()}


def test_the_keeper_setting_that_scans_done_items_is_a_boolean_and_off_by_default(tmp_path):
    from plangraph.keeper.config import Config, ConfigError
    base = {"db": str(tmp_path / "k.db")}
    assert Config(dict(base), tmp_path).deploy_inventory_done is False
    assert Config({**base, "deploy_inventory_done": True}, tmp_path).deploy_inventory_done is True
    with pytest.raises(ConfigError, match="deploy_inventory_done"):
        Config({**base, "deploy_inventory_done": "yes"}, tmp_path)


def test_an_inventory_request_is_refused_while_done_items_are_not_scanned(env):  # noqa: F811
    from plangraph.keeper.errors import Refused
    with pytest.raises(Refused, match="deploy_inventory_done"):
        env.store.close_merged_auto(env.p("system"), require_seats=False, apply=False, inventory=True)
    with pytest.raises(Refused, match="dry run"):
        env.store.include_done_in_scan = True
        env.store.close_merged_auto(env.p("system"), require_seats=False, apply=True, inventory=True)


def _deploy_row(env, key, at):
    item = env.store._item(key)
    env.store.conn.execute(
        "INSERT INTO evidence(key,epoch,kind,ref,verified,detail,at) VALUES(?,?,?,?,?,?,?)",
        (key, item["epoch"], "deploy",
         '{"host":"host-a","is_active":"active","unit":"new.service"}', 1, "fixture", at))


def test_only_deploy_proof_recorded_after_the_merge_clears_a_done_item(env):  # noqa: F811
    _land_with_unit(env.repo.root)
    _done(env, "demo:A", note="Deploy-Hosts: host-a")
    _deploy_row(env, "demo:A", "2020-01-01T00:00:00+00:00")  # verified, but before the merge landed
    rows = {row["key"]: row for row in _inventory(env, inventory=True)["deployment_pending"]}
    assert rows["demo:A"]["missing"] == ["host-a:new.service"]
    _deploy_row(env, "demo:A", "2099-01-01T00:00:00+00:00")
    assert _inventory(env, inventory=True)["deployment_pending"] == []
