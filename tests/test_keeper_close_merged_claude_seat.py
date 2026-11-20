"""LE-CLAUDE-SEAT-CLOSE-MERGED-REFUSALS: close-merged accepts claude-seat evidence."""
import json
from types import SimpleNamespace

import pytest

from test_keeper_close_merged import HEAD, KEY, MERGE, MergedGit, unseated_w  # noqa: F401
from plangraph.keeper import auth, context_sources
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
from plangraph.keeper.evidence_types import Result

HEAD2 = "fedcba9876543210" * 2 + "fedcba98"
MERGE2 = "3" * 40


class FakeClaudeSeat:
    """The writer rule of the Rust transcript verifier, without a transcript."""

    def verify(self, item, ref, ctx):
        writers = item.get("_seat_writer_families", [])
        if not writers:
            return Result(False, "claude_seat_writer_unknown", ref)
        if {"claude", "anthropic"} & set(writers):
            return Result(False, "claude_seat_writer_family", ref)
        return Result(True, "verified", dict(ref, seat={
            "model": "claude-sonnet-5-5", "verdict": "pass", "head": ref["head"],
            "run_id": f"claude-seat-{ref['head'][:8]}"}))


class TwoMerges(MergedGit):
    def merge_commit_of(self, repo, sha, ref):
        return {HEAD: (MERGE, "x@example.invalid"), HEAD2: (MERGE2, "x@example.invalid")}.get(sha)

    def _run(self, repo, args):
        parent = {MERGE: HEAD, MERGE2: HEAD2}[args[-1]]
        return SimpleNamespace(returncode=0, stdout=f"{'2' * 40} {parent}")


def record(root, host, run, body):
    """One mirrored spawner record; the keeper refuses group/other-writable paths."""
    path = root / host / run / "record.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    for parent in (root, root / host, path.parent):
        parent.chmod(0o700)
    path.chmod(0o600)


@pytest.fixture
def seat_w(unseated_w, tmp_path):
    w = unseated_w
    w.mirror = tmp_path / "mirror"
    w.store.ctx["spawner_records"] = SpawnerRecordAdapter(w.mirror)
    w.store.verifiers["claude-seat"] = FakeClaudeSeat()
    token = auth.add_principal(w.conn, "claudeL1", "l1", ["demo"], family="anthropic")
    w.principals["claudeL1"] = auth.authenticate(w.conn, token)
    for key in (KEY, "demo:H"):
        w.conn.execute("UPDATE items SET claimed_by='claudeL1' WHERE key=?", (key,))
    return w


def attach(w, key=KEY, head=HEAD):
    ref = {"transcript": "t", "review_file": "r", "head": head}
    return w.store.add_evidence_router(w.p("router"), key, "claude-seat", ref)


def written_by(w, model, key=KEY, run=None):
    run = run or f"run-writer-{key[-1]}"
    record(w.mirror, "fixture-host", run, {"role": "implement"})
    w.store.record_run(w.p("spawner"), key, {
        "run_id": run, "phase": "finished", "rc": 0, "writer": {"model": model}})


def close(w, key=KEY):
    return w.store.close_merged(w.p("router"), key, "D-5504")


def test_the_claimant_principal_is_not_a_writer_when_a_writer_is_on_record(seat_w):
    written_by(seat_w, "gpt-test")
    assert attach(seat_w)["verified"]
    result = close(seat_w)
    assert result["state"] == "done" and result["reviews_passed"] == 1


def test_a_claude_written_diff_stays_refused(seat_w):
    written_by(seat_w, "gpt-test")
    assert attach(seat_w)["verified"]  # attached while the recorded writer was another family
    written_by(seat_w, "claude-sonnet-5-5", run="run-writer-2")
    with pytest.raises(Refused, match="claude_seat_writer_family"):
        close(seat_w)
    assert seat_w.store._item(KEY)["state"] == "in_review"


def test_with_no_writer_on_record_the_claimant_stands_for_it(seat_w):
    added = attach(seat_w)
    assert not added["verified"] and "claude_seat_writer_family" in added["detail"]


def test_evidence_for_each_merged_part_head_closes_the_item(seat_w, monkeypatch):
    seat_w.store.ctx["git"] = TwoMerges()
    monkeypatch.setattr(context_sources, "plan_item_commits", lambda *a, **kw: [
        {"sha": HEAD, "date": "2026-01-01", "merge": {"pr": 1}},
        {"sha": HEAD2, "date": "2026-01-02", "merge": {"pr": 2}}])
    written_by(seat_w, "gpt-test")
    attach(seat_w, head=HEAD)
    attach(seat_w, head=HEAD2)
    assert close(seat_w)["state"] == "done"


def test_evidence_for_a_head_that_is_not_a_merged_head_still_refuses(seat_w):
    written_by(seat_w, "gpt-test")
    attach(seat_w, head=HEAD)
    attach(seat_w, head="b" * 40)
    with pytest.raises(Refused, match="claude_seat_head"):
        close(seat_w)


def other_seat(w):
    record(w.mirror, "fixture", "run-one", {
        "run_id": "run-one", "host": "fixture", "item_key": "demo:H", "role": "review",
        "status": "completed", "exit_code": 0, "served_model": "gemini-test",
        "finish_reason": "stop", "attempts": [{}], "completion": {"head_sha": HEAD},
        "stdout_tail": "TEST_GAMING: no\nVERDICT: PASS\n"})
    w.decision_path.write_text(f"| D-5504 | demo | GO {HEAD}; Review-Runs: run-one |\n", encoding="utf-8")


def test_a_high_tier_item_closes_on_claude_seat_plus_one_other_seat(seat_w):
    written_by(seat_w, "gpt-test", "demo:H")
    attach(seat_w, "demo:H")
    other_seat(seat_w)
    assert close(seat_w, "demo:H")["reviews_passed"] == 2
    verdicts = seat_w.conn.execute("SELECT payload FROM events WHERE key='demo:H' AND type='review_verdict'")
    assert {json.loads(r[0])["family"] for r in verdicts} == {"anthropic", "google"}


def test_a_high_tier_item_with_only_the_claude_seat_needs_the_second_family(seat_w):
    written_by(seat_w, "gpt-test", "demo:H")
    attach(seat_w, "demo:H")
    with pytest.raises(Refused, match="insufficient_seat_families"):
        close(seat_w, "demo:H")
