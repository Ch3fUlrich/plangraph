"""QC accept shadow hook (RT-P1-JEV-SHADOW): plan_actions records, never decides.

The QC axis answers the same question the rules answer, in shadow: the accept
decision of :func:`plangraph.review.actions.plan_actions` (accept = verdict
``pass``; reject = ``fix`` or ``halt``) is asked of the free Jev leg as the
``qc_accept`` choice question.  These tests are hermetic -- a fake ``ask``, no
model, no network -- and they prove the three hard rules:

* rows are recorded for pass, fix and halt and surface on ``show``/``jev_report``;
* the production call site inside ``plan_actions`` is the one that records them;
* a timeout or an unavailable Jev never blocks ``plan_actions`` and never
  changes the plan it returns.
"""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from keeper_util import World, seed
from plangraph.keeper import fmt, schema
from plangraph.planner import jev_client, jev_questions, shadow as shadow_mod
from plangraph.review.actions import plan_actions

PASS = {"verdict": "pass", "reason": "clean review"}
FIX = {"verdict": "fix", "reason": "flush is racy",
       "findings": [{"finding": "flush can drop a write", "files": ["src/a.py"]}],
       "files": ["src/a.py"]}
HALT = {"verdict": "halt", "reason": "scope changed", "risk_tags": ["auth"]}

FIX_PLAN = {
    "fix_cards": [{"title": "Review fix (P1) #1: flush can drop a write",
                   "finding": "flush can drop a write",
                   "files": ["src/a.py"]}],
    "inbox": [], "pass": False,
}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def fixed_ask(choice):
    """A fake Jev that always answers ``choice`` (else the question's first option)."""
    def ask(state, questions, cfg, *, env=None, opener=None):
        out = {}
        for key, question in questions.items():
            options = list((question.get("criteria") or {}).keys())
            chosen = choice if choice in options else options[0]
            out[key] = jev_client.JevAnswer(status="ok", choice=chosen,
                                            probabilities={chosen: 1.0},
                                            confidence=1.0, reason="ok",
                                            model=cfg.model, latency_ms=1)
        return out
    return ask


def slow_ask(state, questions, cfg, *, env=None, opener=None):
    time.sleep(5)
    return {}


def unavailable_ask(state, questions, cfg, *, env=None, opener=None):
    return {key: jev_client.JevAnswer(status="unavailable", reason="http 503",
                                      model=cfg.model, latency_ms=2)
            for key in questions}


def fake_jev(monkeypatch, ask=None):
    """Turn the real client into a fake Jev: mode ``api`` plus a patched ``ask``."""
    monkeypatch.setattr(jev_client, "ask", ask or fixed_ask("accept"))
    monkeypatch.setenv("PLANGRAPH_JEV", "api")


# ------------------------------------------------------- rows, show, report
def test_qc_rows_for_pass_fix_and_halt_reach_store_show_and_report(w, monkeypatch):
    key, = seed(w, [("A", [])])
    router = w.p("router")

    # three decisions, three fake legs: pass/fix agree, halt disagrees
    fake_jev(monkeypatch, fixed_ask("accept"))
    plan_pass = plan_actions(PASS, "P2", key=key, store=w.store, principal=router)
    fake_jev(monkeypatch, fixed_ask("reject"))
    plan_fix = plan_actions(FIX, "P2", key=key, store=w.store, principal=router)
    fake_jev(monkeypatch, fixed_ask("accept"))
    plan_halt = plan_actions(HALT, "P2", key=key, store=w.store, principal=router)

    # the plans are exactly what the rules plan with Jev off: Jev never decides
    assert plan_pass == {"fix_cards": [], "inbox": [], "pass": True}
    assert len(plan_fix["fix_cards"]) == 1 and plan_fix["pass"] is False
    assert len(plan_halt["inbox"]) == 1 and plan_halt["pass"] is False

    rows = w.store.jev_rows(key)
    assert [r["kind"] for r in rows] == ["qc_accept"] * 3
    assert [r["rule_answer"] for r in rows] == ["accept", "reject", "reject"]
    assert [r["jev_answer"] for r in rows] == ["accept", "reject", "accept"]
    assert [r["status"] for r in rows] == ["ok"] * 3
    assert all(r["attempt"] is None for r in rows)
    assert len({r["decision_id"] for r in rows}) == 3            # one decision each
    assert all(isinstance(r["decision_id"], str) and len(r["decision_id"]) == 32
               for r in rows)
    assert len({r["input_hash"] for r in rows}) == 3             # three different states
    assert all(isinstance(r["input_hash"], str) and len(r["input_hash"]) == 64
               for r in rows)

    # jev_report: the object ``GET /v1/jev_report`` serves, with the qc axis
    report = w.store.jev_report(router)
    assert set(report["kinds"]) == set(schema.JEV_KINDS)
    qc = report["kinds"]["qc_accept"]
    assert (qc["total"], qc["ok"], qc["agree"], qc["disagree"]) == (3, 3, 2, 1)
    assert qc["agreement_rate"] == round(2 / 3, 4)
    assert qc["labels"] == ["accept", "reject"]
    assert qc["kappa"] == 0.4
    assert qc["confusion"] == {"accept": {"accept": 1, "reject": 0},
                               "reject": {"accept": 1, "reject": 1}}
    assert "qc_accept" in fmt.jev_report_text(report)

    # show: the object ``GET /v1/show`` serves, plus its text door
    info = w.store.show(router, key)
    assert [r["kind"] for r in info["jev_shadow"]] == ["qc_accept"] * 3
    assert [r["agree"] for r in info["jev_shadow"]] == [True, True, False]
    assert info["jev_agreement"]["total"] == 3
    assert info["jev_agreement"]["agree"] == 2
    text = fmt.show_text(info)
    assert "jev qc_accept" in text
    assert "jev agree 2/3 (rate 0.67)" in text
    assert "rule=accept jev=accept agree" in text
    assert "rule=reject jev=accept differ" in text


def test_qc_state_and_input_hash_never_carry_the_rules_own_word(w, monkeypatch):
    """The rule's own word is compared, never handed to Jev in the state."""
    key, = seed(w, [("A", [])])
    fake_jev(monkeypatch)
    assert w.store.jev_rows(key) == []

    plan_actions(FIX, "P1", key=key, store=w.store, principal=w.p("router"))

    state = jev_questions.qc_state(FIX, "P1", key)
    for word in ("fix", "reject", "verdict"):
        assert word not in state
    assert "flush is racy" in state            # the evidence Jev decides on
    row, = w.store.jev_rows(key)
    assert row["input_hash"] == hashlib.sha256(state.encode()).hexdigest()
    assert row["rule_answer"] == "reject"
    assert state not in json.dumps(row)        # the state itself never lands in a row


# ------------------------------------------------------ the production call
def test_production_call_site_plan_actions_records_a_qc_accept_row(w, monkeypatch):
    """Fails if the shadow call inside ``plan_actions`` is deleted."""
    key, = seed(w, [("A", [])])
    fake_jev(monkeypatch)
    plan_actions(PASS, "P1", key=key, store=w.store, principal=w.p("router"))
    rows = w.store.jev_rows(key)
    assert [r["kind"] for r in rows] == ["qc_accept"]
    assert rows[0]["rule_answer"] == "accept"
    assert rows[0]["jev_answer"] == "accept"


def test_the_default_hook_resolves_the_jsonl_sink_like_route_with_shadow(
        monkeypatch, tmp_path):
    """The default hook needs no store: the routing hook's log sink works too."""
    fake_jev(monkeypatch)
    log = tmp_path / "jev.jsonl"
    plan_actions(PASS, "P1", key="demo:A", log_path=str(log))

    lines = [json.loads(line)
             for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in lines] == ["qc_accept"]
    assert lines[0]["event"] == "jev"
    assert lines[0]["rule_answer"] == "accept"
    assert lines[0]["card"] == "demo:A"
    assert lines[0]["status"] == "ok"
    assert shadow_mod.summarize_jev(lines)["kinds"]["qc_accept"]["total"] == 1


# ------------------------------------------------------------- never blocks
def test_qc_shadow_is_off_by_default(monkeypatch):
    calls = []

    def tracking(*args, **kwargs):
        calls.append(1)
        raise AssertionError("ask must never run while the flag is off")

    monkeypatch.delenv("PLANGRAPH_JEV", raising=False)
    monkeypatch.setattr(jev_client, "ask", tracking)
    plan_actions(PASS, "P1", key="demo:A")
    plan_actions(FIX, "P1", key="demo:A")
    plan_actions(HALT, "P1", key="demo:A")
    assert calls == []


@pytest.mark.parametrize("variant", ["timeout", "unavailable"])
def test_a_bad_jev_never_blocks_plan_actions_or_changes_the_plan(
        w, monkeypatch, variant):
    key, = seed(w, [("A", [])])
    router = w.p("router")
    monkeypatch.delenv("PLANGRAPH_JEV", raising=False)
    baseline = plan_actions(FIX, "P1", key=key, store=w.store, principal=router)
    assert baseline == FIX_PLAN
    assert w.store.jev_rows(key) == []         # off: nothing asked, nothing recorded

    monkeypatch.setattr(jev_client, "ask",
                        slow_ask if variant == "timeout" else unavailable_ask)
    monkeypatch.setenv("PLANGRAPH_JEV", "api")
    monkeypatch.setenv("PLANGRAPH_JEV_TIMEOUT_S", "0.2")

    started = time.monotonic()
    got = plan_actions(FIX, "P1", key=key, store=w.store, principal=router)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0                       # bounded by the guarded ask
    assert got == baseline                     # the plan is unchanged
    rows = w.store.jev_rows(key)
    assert [r["kind"] for r in rows] == ["qc_accept"]
    assert rows[0]["status"] == variant
    assert rows[0]["jev_answer"] is None
    if variant == "timeout":
        assert rows[0]["reason"].startswith("no answer within")


def test_a_broken_hook_cannot_change_the_plan(monkeypatch):
    def boom(verdict, phase, key):
        raise RuntimeError("hook boom")

    monkeypatch.setenv("PLANGRAPH_JEV", "api")
    assert plan_actions(FIX, "P1", key="demo:A", shadow=boom) == FIX_PLAN
    assert plan_actions(PASS, "P1", key="demo:A", shadow=boom) == {
        "fix_cards": [], "inbox": [], "pass": True}


def test_the_shadow_hook_receives_a_deep_copy():
    seen = []

    def hook(verdict, phase, key):
        seen.append(verdict)
        verdict["verdict"] = "tampered"
        verdict["findings"].append("injected")

    verdict = dict(FIX)
    plan = plan_actions(verdict, "P1", key="demo:A", shadow=hook)
    assert seen[0] is not verdict              # the hook got a copy
    assert verdict == FIX                      # the caller's verdict is untouched
    assert plan == FIX_PLAN
