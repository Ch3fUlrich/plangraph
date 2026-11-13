"""Hermetic D-5558 follow-up: real GO authority clauses and D-5529 seats."""
import socket
import subprocess
from types import SimpleNamespace

import pytest

from test_keeper_close_merged import (HEAD, KEY, _at_write_lock, _close,
                                      _ConnectionProxy, _refused, unseated_w, w)
from plangraph.keeper import auth


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("merged-close regressions must not spawn processes or connect sockets")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


PR_FORMS = ["#786", "#786,", "`#786`", "`#786`,"]


@pytest.mark.parametrize("stamp", ["(18:30Z, date -u)", "(18:30Z)",
                                  "(18:3xZ)", "(18:3xZ, date -u)",
                                  "(~18:30Z)", ""])
@pytest.mark.parametrize("verdict", ["GO", "NO GO"])
def test_all_log_stamp_forms_preserve_go_authority(w, stamp, verdict):
    w.decision_path.write_text(
        f"| D-5504 | demo | {stamp} {verdict} #786; GO-HEAD {HEAD} |\n",
        encoding="utf-8")
    if verdict == "GO":
        assert _close(w)["state"] == "done"
    else:
        _refused(w, "evidence_unverified", "go_row_not_go")


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
@pytest.mark.parametrize("pr", PR_FORMS)
def test_go_head_ignores_later_review_narrative(w, prefix, pr):
    text = (f"{prefix}GO {pr} fixture; GO-HEAD {HEAD}; "
            "no workflow changes. The GO proof was reviewed.")
    w.decision_path.write_text(
        f"| D-5504 | demo | {text.replace(HEAD, 'f' * 40)} |\n", encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")
    w.decision_path.write_text(f"| D-5504 | demo | {text} |\n", encoding="utf-8")
    proof, result = w.store._merged_go_proof(w.store._item(KEY), "D-5504", w.clock())
    assert proof == HEAD and result.ok
    assert _close(w)["state"] == "done"


@pytest.mark.parametrize("verdict", ["NO GO", "NO-GO", "NOT A GO", '"GO', "title GO"])
@pytest.mark.parametrize("pr", PR_FORMS)
def test_timestamp_does_not_turn_non_go_into_authority(w, verdict, pr):
    text = f"(23:21Z, date -u) {verdict} {pr} fixture; GO-HEAD {HEAD}"
    w.decision_path.write_text(f"| D-5504 | demo | {text} |\n", encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_not_go")


@pytest.mark.parametrize("token", [f"GO-HEAD {HEAD[:8]}", f"GO-HEAD {HEAD}0",
                                   f"OTHER-GO-HEAD {HEAD}", f"GO-HEAD x{HEAD}"])
@pytest.mark.parametrize("pr", PR_FORMS)
def test_modern_go_requires_whole_go_head_sha40(w, token, pr):
    w.decision_path.write_text(
        f"| D-5504 | demo | (23:21Z, date -u) GO {pr} fixture; {token} |\n",
        encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


@pytest.mark.parametrize("other", [HEAD, "f" * 40])
@pytest.mark.parametrize("pr", PR_FORMS)
def test_multiple_go_head_bindings_refuse(w, other, pr):
    w.decision_path.write_text(
        f"| D-5504 | demo | (23:21Z, date -u) GO {pr} fixture; "
        f"GO-HEAD {HEAD}; GO-HEAD {other} |\n", encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


@pytest.mark.parametrize("pr", PR_FORMS)
def test_modern_go_head_in_another_cell_does_not_bind(w, pr):
    w.decision_path.write_text(
        f"| D-5504 | GO-HEAD {HEAD} | (23:21Z, date -u) GO {pr} fixture |\n",
        encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


@pytest.mark.parametrize("pr", ["#786x", "#786_x", "`#786", "#786`", "`#786`x"])
def test_malformed_pr_token_cannot_use_go_head(w, pr):
    w.decision_path.write_text(
        f"| D-5504 | demo | GO {pr}; GO-HEAD {HEAD} |\n", encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_head_mismatch")


@pytest.mark.parametrize("prefix", ["", "(23:21Z, date -u) "])
@pytest.mark.parametrize("head", [HEAD[:8], f"`{HEAD}`"])
@pytest.mark.parametrize("negation", ["no GO", "NOT a GO", "not yet a go", "NO-GO"])
def test_legacy_go_head_with_later_negation_refuses(w, prefix, head, negation):
    w.decision_path.write_text(
        f"| D-5504 | demo | {prefix}GO {head}; {negation} |\n", encoding="utf-8")
    _refused(w, "evidence_unverified", "go_row_not_go")


@pytest.mark.parametrize("key,families", [(KEY, ["Anthropic"]),
                                          ("demo:H", ["google", "claude-sonnet"])])
def test_anthropic_seat_counts_over_non_claude_writer(unseated_w, key, families):
    w = unseated_w
    _refused(w, "coverage", "seat_family_not_recorded", key=key, families=families)
    w.store.record_run(w.p("spawner"), key, {
        "run_id": "cross-family", "phase": "finished", "rc": 0,
        "writer": {"family": "openai", "model": "gpt-test"},
        "seats": [{"model": "claude-sonnet", "verdict": "pass"}]})
    if key == "demo:H":
        _refused(w, "coverage", "seat_family_not_recorded", key=key, families=families)
        w.store.record_run(w.p("spawner"), key, {
            "run_id": "cross-family-google", "phase": "finished", "rc": 0,
            "seats": [{"model": "gemini-test", "verdict": "pass"}]})
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
def test_auth_lane_requires_non_anthropic_second_family(unseated_w, key, risk):
    w = unseated_w
    w.store.put_card(w.p("router"), key, {"outcome": "fixture", "risk_tags": [risk]})
    w.store.record_run(w.p("spawner"), key, {
        "run_id": "auth-seats", "phase": "finished", "rc": 0,
        "writer": {"family": "openai", "model": "gpt-test"},
        "seats": [{"model": "claude-sonnet", "verdict": "pass"}]})
    _refused(w, "coverage", "insufficient_non_anthropic_seat_families",
             key=key, families=["google", "anthropic"])
    _refused(w, "coverage", "seat_family_not_recorded",
             key=key, families=["google", "deepseek"])
    w.store.record_run(w.p("spawner"), key, {
        "run_id": "auth-google-seat", "phase": "finished", "rc": 0,
        "seats": [{"model": "gemini-test", "verdict": "pass"}]})
    _refused(w, "coverage", "seat_family_not_recorded",
             key=key, families=["google", "deepseek"])
    w.store.record_run(w.p("spawner"), key, {
        "run_id": "auth-deepseek-seat", "phase": "finished", "rc": 0,
        "seats": [{"model": "deepseek-test", "verdict": "pass"}]})
    assert _close(w, key=key, families=["google", "deepseek"])["state"] == "done"


@pytest.mark.parametrize("key,families", [(KEY, ["google"]),
                                          ("demo:H", ["google", "anthropic"])])
@pytest.mark.parametrize("phase,rc,verdict", [("started", 0, "pass"),
    ("finished", None, "pass"), ("finished", 1, "pass"),
    ("finished", 0, "fail"), ("finished", 0, None)])
def test_unfinished_or_failed_seats_do_not_back_close(unseated_w, key, families,
                                                     phase, rc, verdict):
    w = unseated_w
    w.store.record_run(w.p("spawner"), key, {
        "run_id": "invalid-backing", "phase": phase, "rc": rc,
        "seats": [{"model": "gemini-test", "verdict": verdict},
                  {"model": "claude-sonnet", "verdict": verdict}]})
    _refused(w, "coverage", "seat_family_not_recorded", key=key, families=families)


@pytest.mark.parametrize("raw", ["null", "{}", '"auth"', "42", "true", "", "[",
    '["Auth"]', '["SECRETS"]', '[" auth "]', '["unknown"]', '[null]', '[{}]', '[[]]',
    '[false]', '["auth", 1]'])
def test_invalid_card_risk_tags_are_named_audited_refusals(w, raw):
    w.store.put_card(w.p("router"), KEY, {"outcome": "fixture", "risk_tags": []})
    w.conn.execute("UPDATE cards SET risk_tags=? WHERE key=?", (raw, KEY))
    _refused(w, "validation", "card_risk_tags_invalid")


@pytest.mark.parametrize("key,families,code,reason", [
    (KEY, [], "coverage", "insufficient_seat_families"),
    ("demo:H", ["google"], "coverage", "insufficient_seat_families"),
    ("demo:H", ["google", "deepseek"], "validation", "card_risk_tags_invalid")])
def test_invalid_card_tags_follow_tier_seat_minimum(w, key, families, code, reason):
    w.store.put_card(w.p("router"), key, {"outcome": "fixture", "risk_tags": []})
    w.conn.execute("UPDATE cards SET risk_tags='null' WHERE key=?", (key,))
    _refused(w, code, reason, key=key, families=families)


@pytest.mark.parametrize("raw,code,reason", [
    ('["auth"]', "coverage", "insufficient_non_anthropic_seat_families"),
    ("null", "validation", "card_risk_tags_invalid")])
def test_card_risk_tags_are_rechecked_inside_write_lock(w, monkeypatch, raw, code, reason):
    w.store.put_card(w.p("router"), KEY, {"outcome": "fixture", "risk_tags": []})

    def swapped():
        assert w.conn.in_transaction, "inject the changed tags only after the write lock"
        w.conn.execute("UPDATE cards SET risk_tags=? WHERE key=?", (raw, KEY))

    entered = _at_write_lock(w, monkeypatch, swapped)
    _refused(w, code, reason)
    assert entered == ["BEGIN IMMEDIATE"]
    assert not w.conn.in_transaction


def test_null_card_risk_tags_are_named_audited_refusal(w, monkeypatch):
    # SQL normally forbids NULL; an adapter returning it must still fail closed.
    class NullTagsConnection(_ConnectionProxy):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("SELECT risk_tags FROM cards"):
                return SimpleNamespace(fetchone=lambda: {"risk_tags": None})
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(w.store, "conn", NullTagsConnection(w.conn))
    assert w.store.conn.row_factory is w.conn.row_factory
    w.store.conn.commit()
    _refused(w, "validation", "card_risk_tags_invalid")


@pytest.mark.parametrize("tags", [[], ["money"], ["migration"], ["public-contract"],
                                  ["live-step"]])
def test_valid_non_auth_card_tags_keep_standard_seat_count(w, tags):
    w.store.put_card(w.p("router"), KEY, {"outcome": "fixture", "risk_tags": tags})
    assert _close(w)["state"] == "done"


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
    before = w.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    _refused(w, "evidence_unverified", "merged_on_default_missing")
    assert w.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before + 1
    row = w.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["actor"], row["role"], row["action"], row["key"], row["ok"],
            row["code"], row["msg"]) == (
        "router", "router", "close_merged", KEY, 0,
        "evidence_unverified", "merged_on_default_missing")
