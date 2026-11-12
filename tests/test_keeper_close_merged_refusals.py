"""Fail-closed and concurrency contracts for the router-only merged close."""
from contextlib import contextmanager
from types import SimpleNamespace
import pytest

from test_keeper_close_merged import (HEAD, KEY, MERGE, _close,
                                      _counts, _refused, doors, w)


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
    original = w.store._write

    @contextmanager
    def changed():
        w.conn.execute(f"UPDATE items SET {field}=? WHERE key=?", (value, KEY))
        with original():
            yield

    monkeypatch.setattr(w.store, "_write", changed)
    _refused(w, "conflict", "changed under the transaction")


def test_lease_touch_does_not_conflict(w, monkeypatch):
    original = w.store._write

    @contextmanager
    def touched():
        w.conn.execute("UPDATE items SET updated_at='touch', lease_until='touch' WHERE key=?",
                       (KEY,))
        with original():
            yield

    monkeypatch.setattr(w.store, "_write", touched)
    assert _close(w)["state"] == "done"


def test_merged_head_change_conflicts(w, monkeypatch):
    original = w.store._write
    new_head = HEAD[:8] + "0" * 32

    @contextmanager
    def changed():
        monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits",
                            lambda *a, **kw: [{"sha": new_head}])
        git = w.store.ctx["git"]
        monkeypatch.setattr(git, "merge_commit_of", lambda *a: (MERGE, "fixture"))
        monkeypatch.setattr(git, "_run", lambda *a: type("Reply", (), {
            "returncode": 0, "stdout": f"{'2' * 40} {new_head}"})())
        with original():
            yield

    monkeypatch.setattr(w.store, "_write", changed)
    _refused(w, "conflict", "merged_on_default_changed")


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
    assert status == 400 and response["error"] == "validation"
    assert _counts(w) == (before[0], before[1], before[2] + 1)
    row = w.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["action"], row["ok"], row["code"]) == ("close_merged", 0, "validation")
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
    from git_helpers import git_cli
    from plangraph.keeper import context_sources
    from plangraph.keeper.evidence_git import GitAdapter

    git_cli(tmp_path, monkeypatch)  # isolate the production adapter's git config too
    for name, value in {"GIT_AUTHOR_NAME": "Fixture", "GIT_COMMITTER_NAME": "Fixture",
                        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                        "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}.items():
        monkeypatch.setenv(name, value)
    git, init = git_cli(tmp_path)
    repo = tmp_path / "fixture-repo"
    init(repo, "-q", "-b", "main")
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
