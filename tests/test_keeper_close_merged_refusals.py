"""Fail-closed and concurrency contracts for the router-only merged close."""
from types import SimpleNamespace
import pytest

from test_keeper_close_merged import (HEAD, KEY, MERGE, _at_write_lock, _close,
                                      _counts, _refused, doors, unseated_w, w)


def test_unknown_key_refuses(w):
    _refused(w, "not_found", "no item", key="demo:MISSING")


def test_non_review_tier_refuses(w):
    w.conn.execute("UPDATE items SET review_tier='none' WHERE key=?", (KEY,))
    _refused(w, "bad_state", "review_tier=standard or high")


@pytest.mark.parametrize("decision", [None, "", "5504", "D-x", 5504, [], {}])
def test_router_malformed_decision_refuses(w, decision):
    _refused(w, "validation", "decision_id must look like D-NNN", decision=decision)


@pytest.mark.parametrize("source", ["writer_model", "run-model", "run-family"])
def test_unknown_writer_refuses(w, source):
    if source == "writer_model":
        w.conn.execute("UPDATE items SET writer_model='foo' WHERE key=?", (KEY,))
        reason = "recorded writer model family unknown"
    else:
        writer = {"model": "foo"} if source == "run-model" else {
            "model": "gemini-test", "family": "foo"}
        w.store.record_run(w.p("spawner"), KEY, {
            "run_id": "unknown-writer", "phase": "finished", "rc": 0, "writer": writer})
        w.conn.execute("UPDATE items SET writer_model=NULL WHERE key=?", (KEY,))
        reason = "recorded run writer family unknown"
    _refused(w, "writer_unknown", reason)


@pytest.mark.parametrize("phase,rc,verdict", [
    ("started", 0, "pass"), ("finished", 1, "pass"),
    ("finished", 0, "fail"), ("finished", 0, "invalid"), ("finished", 0, None)])
def test_seats_must_have_recorded_successful_runs(w, phase, rc, verdict):
    w.store.record_run(w.p("spawner"), KEY, {
        "run_id": "unproven-seat", "phase": phase, "rc": rc,
        "seats": [{"model": "gpt-test", "verdict": verdict}]})
    _refused(w, "coverage", "seat_family_not_recorded", families=["openai"])


@pytest.mark.parametrize("field,value", [("epoch", 99), ("round", 99),
                                         ("review_tier", "high"),
                                         ("writer_model", "deepseek-test")])
def test_item_proof_changes_conflict(w, monkeypatch, field, value):
    def changed():
        w.conn.execute(f"UPDATE items SET {field}=? WHERE key=?", (value, KEY))

    entered = _at_write_lock(w, monkeypatch, changed)
    _refused(w, "conflict", "changed under the transaction")
    assert entered == ["BEGIN IMMEDIATE"]
    assert not w.conn.in_transaction


def test_lease_touch_does_not_conflict(w, monkeypatch):
    def touched():
        w.conn.execute("UPDATE items SET updated_at='touch', lease_until='touch' WHERE key=?",
                       (KEY,))

    entered = _at_write_lock(w, monkeypatch, touched)
    assert _close(w)["state"] == "done"
    assert entered == ["BEGIN IMMEDIATE"]
    assert not w.conn.in_transaction


def test_merged_head_change_conflicts(w, monkeypatch):
    new_head = HEAD[:8] + "0" * 32

    def changed():
        monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits",
                            lambda *a, **kw: [{"sha": new_head}])
        git = w.store.ctx["git"]
        monkeypatch.setattr(git, "merge_commit_of", lambda *a: (MERGE, "fixture"))
        monkeypatch.setattr(git, "_run", lambda *a: type("Reply", (), {
            "returncode": 0, "stdout": f"{'2' * 40} {new_head}"})())

    entered = _at_write_lock(w, monkeypatch, changed)
    _refused(w, "conflict", "merged_on_default_changed")
    assert entered == ["BEGIN IMMEDIATE"]
    assert not w.conn.in_transaction


def test_followup_commit_does_not_replace_merged_head(w, monkeypatch):
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits",
                        lambda *a, **kw: [{"sha": "f" * 40}, {"sha": HEAD}])
    assert _close(w)["state"] == "done"
    assert w.store._evidence(KEY)[-1]["ref"]["merged_head"] == HEAD


def test_later_merged_followup_does_not_rebind_go(w, monkeypatch):
    later = "f" * 40
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits",
                        lambda *a, **kw: [{"sha": later}, {"sha": HEAD}])
    git = w.store.ctx["git"]
    monkeypatch.setattr(git, "merge_commit_of", lambda repo, sha, ref: (sha, "fixture"))
    monkeypatch.setattr(git, "_run", lambda repo, args: SimpleNamespace(
        returncode=0, stdout=f"{'2' * 40} {args[-1]}"))
    assert _close(w)["state"] == "done"
    assert w.store._evidence(KEY)[-1]["ref"]["merged_head"] == HEAD


def test_ambiguous_go_head_prefix_refuses(w, monkeypatch):
    other = HEAD[:8] + "0" * 32
    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits",
                        lambda *a, **kw: [{"sha": other}, {"sha": HEAD}])
    git = w.store.ctx["git"]
    monkeypatch.setattr(git, "merge_commit_of", lambda repo, sha, ref: (sha, "fixture"))
    monkeypatch.setattr(git, "_run", lambda repo, args: SimpleNamespace(
        returncode=0, stdout=f"{'2' * 40} {args[-1]}"))
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


def test_go_in_another_cell_cannot_authorize_close(w):
    w.decision_path.write_text(f"| D-5504 | GO {HEAD} | HOLD {HEAD} |\n")
    _refused(w, "evidence_unverified", "go_row_not_go")


def test_unrelated_hex_in_decision_cell_cannot_bind_head(w):
    w.decision_path.write_text(f"| D-5504 | demo | GO 12345678; previous {HEAD} |\n")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


@pytest.mark.parametrize("body", [
    {}, {"key": KEY}, {"key": KEY, "decision_id": "D-5504"},
    {"key": [], "decision_id": "D-5504", "families": ["google"]},
    {"key": KEY, "decision_id": {}, "families": ["google"]},
    {"key": KEY, "decision_id": "D-5504", "families": None},
    {"key": KEY, "decision_id": "D-5504", "families": "google"}])
def test_router_http_malformed_body_is_audited_refusal(w, doors, body):
    before = _counts(w)
    status, response = doors.http("router", "POST", "/v1/close_merged", body)
    automatic = body.get("key") == KEY and body.get("decision_id") == "D-5504" \
        and body.get("families") is None
    assert status == 400
    assert response["error"] == ("evidence_unverified" if automatic else "validation")
    if automatic:
        assert "NO-WITNESS" in response["msg"]
    assert _counts(w) == (before[0], before[1], before[2] + 1)
    row = w.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["action"], row["ok"], row["code"]) == ("close_merged", 0, response["error"])
    assert row["key"] == (body.get("key") if isinstance(body.get("key"), str) else None)
    assert w.store._item(KEY)["state"] == "in_review"


def test_historical_done_lock_refuses(w, monkeypatch):
    _close(w)
    # SQL locks normally make this stale-cache shape impossible; inject only
    # the read result to prove the event lock is still enforced independently.
    original = w.store._get_or_refuse
    monkeypatch.setattr(w.store, "_get_or_refuse",
                        lambda key: {**original(key), "state": "in_review"})
    _refused(w, "locked", "DONE")


@pytest.mark.parametrize("proof", [None, ("bad", "fixture"),
    SimpleNamespace(returncode=1, stdout=""),
    SimpleNamespace(returncode=0, stdout=HEAD),
    SimpleNamespace(returncode=0, stdout=f"bad {HEAD}"), RuntimeError("unavailable")])
def test_unproven_merge_refuses(w, monkeypatch, proof):
    git = w.store.ctx["git"]
    if isinstance(proof, SimpleNamespace):
        monkeypatch.setattr(git, "_run", lambda *a: proof)
    elif isinstance(proof, Exception):
        def unavailable(*args):
            raise proof
        monkeypatch.setattr(git, "merge_commit_of", unavailable)
    else:
        monkeypatch.setattr(git, "merge_commit_of", lambda *a: proof)
    _refused(w, "evidence_unverified", "merged_on_default_missing")


def test_real_git_merge_head_survives_later_trailer_commit(w, tmp_path, monkeypatch):
    import os
    import subprocess
    from plangraph.keeper import context_sources
    from plangraph.keeper.evidence_git import GitAdapter

    required_env = {
        "GIT_AUTHOR_NAME": "Fixture", "GIT_COMMITTER_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    }
    real_run = subprocess.run

    def checked_run(args, **kwargs):
        if args[0] == "git":
            env = kwargs.get("env", os.environ)
            assert all(env.get(k) == v for k, v in required_env.items()), (
                "every git call must have explicit fixture identity and isolated config")
            if "init" in args:
                assert "init.defaultBranch=main" in args
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", checked_run)
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "git-home"
    home.mkdir()
    for name, value in {**required_env, "HOME": str(home),
                        "XDG_CONFIG_HOME": str(home)}.items():
        monkeypatch.setenv(name, value)

    def git(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True,
                              env=os.environ.copy()).stdout.strip()

    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    git(repo, "-c", "init.defaultBranch=main", "init", "-q")
    git(repo, "commit", "--allow-empty", "-m", "initial")
    git(repo, "switch", "-c", "lane")
    git(repo, "commit", "--allow-empty", "-m", f"work\n\nPlan-Item: {KEY}")
    git(repo, "commit", "--allow-empty", "-m", "PR head without trailer")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    git(repo, "merge", "--no-ff", "-m", "Merge PR #1", "lane")
    git(repo, "commit", "--allow-empty", "-m", f"later docs\n\nPlan-Item: {KEY}")
    followup = git(repo, "rev-parse", "HEAD")
    adapter = GitAdapter()
    monkeypatch.setattr(adapter, "repo_path", lambda project: str(repo))
    monkeypatch.setattr(adapter, "repo_ref", lambda project: "main")
    monkeypatch.setattr(context_sources, "plan_item_commits", w.real_plan_item_commits)
    w.store.ctx["git"] = adapter
    w.decision_path.write_text(f"| D-5504 | demo | GO {followup} |\n")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")
    w.decision_path.write_text(f"| D-5504 | demo | GO {head} |\n")
    assert _close(w)["state"] == "done"
    assert w.store._evidence(KEY)[-1]["ref"]["merged_head"] == head
