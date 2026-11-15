"""Read verbs: ready, show, blocked, waves, critical_path, percent, alerts,
digest, snapshot, whoami — all built on plangraph.views."""
import pytest

from keeper_util import World, seed, to_claimed, to_done, to_in_review
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", ["A"])])
    yield world
    world.close()


def test_ready_returns_views_brief_text(w):
    text = w.store.ready(w.p("reader"))
    assert "demo:A" in text and "demo:B" not in text


def test_ready_project_filter(w):
    assert w.store.ready(w.p("reader"), project="nope") == ""


def test_show_compact(w):
    key = "demo:A"
    epoch = to_claimed(w, key)
    w.store.add_evidence(w.p("l1main"), key, epoch, "decision", {"id": "D-1"})
    info = w.store.show(w.p("reader"), key)
    assert info["key"] == key and info["state"] == "claimed"
    assert info["epoch"] == epoch
    assert info["lease_until"] and info["claimed_by"] == "l1main"
    assert "demo:B" in info["unblocks"]
    assert info["evidence"][0]["kind"] == "decision"
    assert info["depends_on"] == []


def test_blocked(w):
    assert w.store.blocked(w.p("reader")) == {
        "demo:B": ["demo:A"], "demo:GOAL": ["demo:A", "demo:B"]}


def test_waves(w):
    assert w.store.waves(w.p("reader")) == [["demo:A"], ["demo:B"], ["demo:GOAL"]]


def test_critical_path(w):
    path = w.store.critical_path(w.p("reader"))
    assert path["keys"][0] == "demo:A"
    assert path["hours"] >= 2


def test_percent(w):
    value = w.store.percent(w.p("reader"), by="count")
    assert 0.0 <= value <= 100.0


def test_whoami(w):
    who = w.store.whoami(w.p("l1main"))
    assert who == {"name": "l1main", "role": "l1", "projects": ["demo"],
                   "family": "teama"}


def test_snapshot_head_and_fields(w):
    snap = w.store.snapshot(w.p("reader"))
    head = w.conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    assert snap["head"] == (head[0] if head else None)
    item = {it["key"]: it for it in snap["items"]}["demo:A"]
    assert {"epoch", "evidence", "updated_at", "corrected_by"} <= set(item)


def test_snapshot_annotates_corrected_by(w):
    to_done(w, "demo:A")
    items = [{"project": "demo", "id": "FIX", "title": "fix", "kind": "lane",
              "state": "open", "depends_on": [], "corrects": "demo:A",
              "evidence_kinds": ["any"]},
             {"project": "demo", "id": "GOAL2", "kind": "goal", "state": "open",
              "depends_on": ["demo:FIX"], "evidence_kinds": ["any"]}]
    w.store.create_items(w.p("router"), items)
    snap = w.store.snapshot(w.p("reader"))
    a = {it["key"]: it for it in snap["items"]}["demo:A"]
    assert a["corrected_by"] == ["demo:FIX"]


# ------------------------------------------------------------------ alerts

def test_alerts_flags_expiring_lease(w):
    to_claimed(w, "demo:A", lease_s=600)
    alerts = w.store.alerts(w.p("reader"))
    assert [x["key"] for x in alerts["lease_expiring"]] == ["demo:A"]


def test_alerts_flags_stall_after_cancel(w):
    w.store.cancel(w.p("router"), "demo:A", "dropped")
    alerts = w.store.alerts(w.p("reader"))
    assert alerts["stalled"] is True


def test_alerts_flags_repeated_refusals(w):
    for _ in range(2):
        with pytest.raises(Refused):
            w.store.claim(w.p("reader"), "demo:A")
    alerts = w.store.alerts(w.p("reader"))
    refusals = {x["key"]: x["count"] for x in alerts["refusals"]}
    assert refusals.get("demo:A", 0) >= 2


# ------------------------------------------------------------------ digest

def test_digest_router_only(w):
    with pytest.raises(Refused) as exc:
        w.store.digest(w.p("reader"))
    assert exc.value.code == "forbidden"


def test_digest_counts_and_done(w):
    to_done(w, "demo:A")
    digest = w.store.digest(w.p("router"))
    assert digest["projects"]["demo"]["done"] >= 1
    assert "demo:A" in [d["key"] for d in digest["done"]]


# ------------------------------------------------------------------- audit

def test_audit_records_success_and_refusal(w):
    to_claimed(w, "demo:A")
    with pytest.raises(Refused):
        w.store.claim(w.p("reader"), "demo:A")
    rows = [tuple(r) for r in w.conn.execute(
        "SELECT action, ok, code FROM audit_log ORDER BY id").fetchall()]
    assert ("claim", 1, None) in rows
    assert any(action == "claim" and ok == 0 and code == "forbidden"
               for action, ok, code in rows)


def test_ready_partitions_claim_staleness_and_open_pr_without_item_io(tmp_path, monkeypatch):
    import json
    import subprocess
    from keeper_util import defn
    from plangraph.keeper import context_sources, fmt
    from plangraph import views
    from plangraph.keeper.evidence_git import GitAdapter
    from plangraph.keeper.evidence_decisions import DecisionsAdapter
    from test_task_context_guard import _git, _commit, _decide

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "--allow-empty", "-qm", "base")
    # Supply identity per invocation without altering repository configuration.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")
    _commit(repo, "merged", "done\n\nPlan-Item: demo:MERGED")
    _git(repo, "checkout", "-qb", "unrelated-branch-name")
    head = _commit(repo, "pending", "pending\n\nPlan-Item: demo:PENDING")
    _git(repo, "checkout", "-q", "main")
    decisions = tmp_path / "decisions.md"
    decisions.write_text("| ID | project | decision |\n")
    _decide(decisions, 12, "demo:STALE premise changed")
    git = GitAdapter({"demo": {"path": str(repo), "ref": "main"}})
    real_run, real_subprocess = git._run, subprocess.run
    calls = []

    def run(repo_path, args, **kw):
        calls.append(args)
        if args[:3] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, "https://github.com/example/repo\n", "")
        if args[0] == "fetch":
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(repo_path, args, **kw)

    def hosted(args, **kw):
        if args[0] == "gh":
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps([
                {"number": 71, "headRefOid": head, "headRefName": "unrelated-branch-name"}]), "")
        return real_subprocess(args, **kw)

    monkeypatch.setattr(git, "_run", run)
    monkeypatch.setattr(subprocess, "run", hosted)
    world = World(tmp_path)
    try:
        world.store.ctx = {"git": git, "decisions": DecisionsAdapter(decisions)}
        names = ["MERGED", "STALE", "PENDING"] + [f"CLEAN-{i}" for i in range(30)]
        world.store.create_items(world.p("router"), [
            *[defn(name, description="per D-10") for name in names],
            defn("GOAL", names, kind="goal")])
        # The ready path must never fall back to item-by-item git probes.
        def item_io(*args, **kwargs):
            pytest.fail("per-item git call in ready")
        monkeypatch.setattr(context_sources, "plan_item_commits", item_io)
        monkeypatch.setattr(context_sources, "open_lanes", item_io)
        groups = world.store._ready_groups(n=1)
        assert [r["key"] for r in groups["items"]] == ["demo:CLEAN-0"]
        reasons = {r["key"]: r["reason"] for r in groups["needs_reconcile"]}
        assert "already merged" in reasons["demo:MERGED"]
        assert "premise outdated: D-12" in reasons["demo:STALE"]
        assert [(r["key"], r["prs"]) for r in groups["in_progress"]] == [
            ("demo:PENDING", [71])]
        text = fmt.ready_text(groups["items"], server_items=True,
                              unblocks=views.unblocks(world.store._all_items()),
                              needs_reconcile=groups["needs_reconcile"],
                              in_progress=groups["in_progress"])
        assert "NEEDS_RECONCILE:" in text and "IN_PROGRESS: PR #71" in text
        assert sum(a[0] == "log" for a in calls) == 1
        assert sum(a[0] == "gh" for a in calls) == 1
        def no_io(*args, **kwargs):
            pytest.fail("cached ready performed subprocess I/O")
        monkeypatch.setattr(git, "_run", no_io)
        monkeypatch.setattr(subprocess, "run", no_io)
        assert len(world.store._ready_groups(n=None)["items"]) == 30
        assert world.store.ready(world.p("reader"), n=1) == text
    finally:
        world.close()


def test_ready_refreshes_origin_instead_of_using_a_stale_local_branch(tmp_path, monkeypatch):
    from plangraph.keeper import context_sources
    from plangraph.keeper.evidence_git import GitAdapter
    from test_task_context_guard import _git, _commit
    for name, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.invalid"),
                        ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.invalid")):
        monkeypatch.setenv(name, value)
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _commit(origin, "base", "base")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    sha = _commit(origin, "merged", "done\n\nPlan-Item: demo:A")
    adapter = GitAdapter({"demo": {"path": str(checkout), "ref": "main"}})
    world = World(tmp_path)
    try:
        seed(world, [("A", [])])
        world.store.ctx = {"git": adapter}
        groups = world.store._ready_groups()
        assert groups["items"] == []
        assert sha[:8] in groups["needs_reconcile"][0]["reason"]
        assert _git(checkout, "rev-parse", "main") != sha
        # Claim reads the same authority and preserves its audited override.
        with pytest.raises(Refused, match="already merged"):
            world.store.claim(world.p("l1main"), "demo:A")
        assert context_sources.indexed_work(
            context_sources.work_index(adapter, "demo"), "demo:A")["commits"][0]["sha"] == sha
    finally:
        world.close()


def test_ready_and_claim_keep_partial_git_adapters_best_effort(w, tmp_path):
    class PartialGit:
        def repo_path(self, project):
            return str(tmp_path)

        def repo_ref(self, project):
            return "main"

    w.store.ctx = {"git": PartialGit()}
    assert [r["key"] for r in w.store._ready_groups()["items"]] == ["demo:A"]
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1
