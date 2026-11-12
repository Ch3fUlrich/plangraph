"""Hermetic D-5558 follow-up: real GO authority clauses and D-5529 seats."""
import subprocess
from types import SimpleNamespace

import pytest

from test_keeper_close_merged import HEAD, KEY, _close, _refused, w
from plangraph.keeper import auth


# Verbatim authority clauses from the router decision log on origin/main,
# router/docs/decisions-log.md (linked by router/DECISIONS.md). The remaining
# review narrative is omitted: it is not part of the head-bound GO authority.
GO_ROWS = [
    ("D-5543", "a3888c1947371cfa5d40df34b79c419958cecc2a",
     "| D-5543 | plangraph | (22:52Z, date -u) GO #786 CI-SELECT-RUST-ON-SCHEMAS, "
     "merge only by plangraph-replan through merge-part; "
     "GO-HEAD a3888c1947371cfa5d40df34b79c419958cecc2a; a moved head voids this. |"),
    ("D-5545", "94c806bf6e6b443c767599a60d5334d67958acba",
     "| D-5545 | plangraph | (23:02Z, date -u) GO #754 UTILITY-EFFORT-ROWS routing half, "
     "merge only by plangraph-replan through merge-part; "
     "GO-HEAD 94c806bf6e6b443c767599a60d5334d67958acba; a moved head voids this. |"),
    ("D-5558", "7628b117c02eee861bbd423ecf5f4fc70a5d792d",
     "| D-5558 | plangraph | (23:21Z, date -u) GO #781 INREVIEW-CLOSE-ROUTER-PATH "
     "slice A (keeper verb close_merged), merge only by plangraph-replan through merge-part; "
     "GO-HEAD 7628b117c02eee861bbd423ecf5f4fc70a5d792d; a moved head voids this. |"),
]


@pytest.mark.parametrize("decision,head,row", GO_ROWS, ids=[r[0] for r in GO_ROWS])
def test_real_router_go_row_closes_only_its_merged_head(w, monkeypatch, decision, head, row):
    w.decision_path.write_text(row + "\n", encoding="utf-8")
    # The recorded GO head is initially another head: no unrelated hex may bind.
    _refused(w, "evidence_unverified", "go_row_head_mismatch", decision=decision)
    monkeypatch.setattr(w.store.ctx["git"], "_run", lambda repo, args: SimpleNamespace(
        returncode=0, stdout=f"{'2' * 40} {head}"))
    proof, result = w.store._merged_go_proof(w.store._item(KEY), decision, w.clock())
    assert proof == head and result.ok
    assert _close(w, decision=decision)["state"] == "done"
    assert w.store._evidence(KEY)[-1]["ref"]["merged_head"] == head


@pytest.mark.parametrize("prefix", ["", "(23:21Z, date -u) "])
def test_go_head_ignores_later_review_narrative(w, prefix):
    text = (f"{prefix}GO #1 fixture; GO-HEAD {HEAD}; "
            "no workflow changes. The GO proof was reviewed.")
    w.decision_path.write_text(f"| D-5504 | demo | {text} |\n", encoding="utf-8")
    assert _close(w)["state"] == "done"


@pytest.mark.parametrize("verdict", ["NO GO", "NO-GO", "NOT A GO", '"GO', "title GO"])
def test_timestamp_does_not_turn_non_go_into_authority(w, verdict):
    text = f"(23:21Z, date -u) {verdict} #1 fixture; GO-HEAD {HEAD}"
    w.decision_path.write_text(f"| D-5504 | demo | {text} |\n", encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_not_go")


@pytest.mark.parametrize("token", [f"GO-HEAD {HEAD[:8]}", f"GO-HEAD {HEAD}0",
                                   f"OTHER-GO-HEAD {HEAD}", f"GO-HEAD x{HEAD}"])
def test_modern_go_requires_whole_go_head_sha40(w, token):
    w.decision_path.write_text(
        f"| D-5504 | demo | (23:21Z, date -u) GO #1 fixture; {token} |\n",
        encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


@pytest.mark.parametrize("other", [HEAD, "f" * 40])
def test_multiple_go_head_bindings_refuse(w, other):
    w.decision_path.write_text(
        f"| D-5504 | demo | (23:21Z, date -u) GO #1 fixture; "
        f"GO-HEAD {HEAD}; GO-HEAD {other} |\n", encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


def test_modern_go_head_in_another_cell_does_not_bind(w):
    w.decision_path.write_text(
        f"| D-5504 | GO-HEAD {HEAD} | (23:21Z, date -u) GO #1 fixture |\n",
        encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


@pytest.mark.parametrize("key,families", [(KEY, ["Anthropic"]),
                                          ("demo:H", ["google", "claude-sonnet"])])
def test_anthropic_seat_counts_over_non_claude_writer(w, key, families):
    w.store.record_run(w.p("spawner"), key, {
        "run_id": "cross-family", "phase": "finished", "rc": 0,
        "writer": {"family": "openai", "model": "gpt-test"},
        "seats": [{"model": "claude-sonnet", "verdict": "pass"}]})
    out = _close(w, key=key, families=families)
    assert out["state"] == "done" and out["reviews_passed"] == len(families)
    assert "anthropic" in w.store._evidence(key)[-1]["ref"]["families"]


@pytest.mark.parametrize("source", ["writer_model", "owner_family", "claimant",
                                    "delegate", "run", "commit"])
def test_anthropic_seat_refuses_every_claude_writer_source(w, monkeypatch, source):
    if source == "writer_model":
        w.conn.execute("UPDATE items SET writer_model='claude-sonnet' WHERE key=?", (KEY,))
    elif source == "owner_family":
        w.conn.execute("UPDATE items SET owner_family='anthropic' WHERE key=?", (KEY,))
    elif source in ("claimant", "delegate"):
        auth.add_principal(w.conn, "claudeWriter", "worker", ["demo"], family="anthropic")
        field = "claimed_by" if source == "claimant" else "delegate"
        w.conn.execute(f"UPDATE items SET {field}='claudeWriter' WHERE key=?", (KEY,))
    elif source == "run":
        w.store.record_run(w.p("spawner"), KEY, {
            "run_id": "claude-writer", "phase": "finished", "rc": 0,
            "writer": {"family": "anthropic", "model": "claude-sonnet"}})
        w.conn.execute("UPDATE items SET writer_model=NULL WHERE key=?", (KEY,))
    else:
        monkeypatch.setattr("plangraph.keeper.store_waiver.writer_families",
                            lambda *args: ["anthropic"])
    w.store.record_run(w.p("spawner"), KEY, {
        "run_id": "claude-seat", "phase": "finished", "rc": 0,
        "seats": [{"model": "claude-sonnet", "verdict": "pass"}]})
    _refused(w, "same_family", "writer_seat_family", families=["anthropic"])


@pytest.mark.parametrize("key", [KEY, "demo:H"])
@pytest.mark.parametrize("risk", ["auth", "secrets"])
def test_auth_lane_requires_non_anthropic_second_family(w, key, risk):
    w.store.put_card(w.p("router"), key, {"outcome": "fixture", "risk_tags": [risk]})
    w.store.record_run(w.p("spawner"), key, {
        "run_id": "auth-seats", "phase": "finished", "rc": 0,
        "writer": {"family": "openai", "model": "gpt-test"},
        "seats": [{"model": "claude-sonnet", "verdict": "pass"}]})
    _refused(w, "coverage", "insufficient_non_anthropic_seat_families",
             key=key, families=["google", "anthropic"])
    assert _close(w, key=key, families=["google", "deepseek"])["state"] == "done"


@pytest.mark.parametrize("in_lock", [False, True])
@pytest.mark.parametrize("error", [OSError("git unavailable"),
                                    subprocess.CalledProcessError(1, ["git", "log"])])
def test_plan_item_commits_failure_is_named_audited_refusal(w, monkeypatch, in_lock, error):
    def commits(*args, **kwargs):
        assert kwargs == {"fetch": False}
        if not in_lock or w.conn.in_transaction:
            raise error
        return [{"sha": HEAD}]

    monkeypatch.setattr("plangraph.keeper.context_sources.plan_item_commits", commits)
    _refused(w, "evidence_unverified", "merged_on_default_missing")
