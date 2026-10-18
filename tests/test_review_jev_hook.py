"""Tests for the QC-accept Jev hook: ``review.actions.plan_actions`` + ``qc_hook``.

Jev answers the SAME accept/reject question as the rules in SHADOW: the hook is
observation only, so the ``{"fix_cards", "inbox", "pass"}`` plan returned by
``plan_actions`` is identical with and without it.  Hermetic: stub mode, or ``api``
mode with ``jev_client.ask`` monkeypatched and a ``cfg.url`` on a closed
127.0.0.1 port plus explicit non-existent ``PLANGRAPH_JEV_AUTH_PATH`` /
``PLANGRAPH_JEV_AUTH_DB`` overrides, so no test can reach the real key or the
network.
"""
from __future__ import annotations

import copy
import socket
import time

import pytest

from keeper_util import World, seed
from plangraph.planner import jev_client, jev_shadow
from plangraph.review import actions

PASS = {"fix_cards": [], "inbox": [], "pass": True}
NOTHING = {"fix_cards": [], "inbox": [], "pass": False}


@pytest.fixture(autouse=True)
def _no_ambient_jev(monkeypatch):
    """The real environment must never turn Jev on for these tests."""
    monkeypatch.delenv("PLANGRAPH_JEV", raising=False)


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


# --------------------------------------------------------------------- helpers
def closed_url():
    """A URL on a 127.0.0.1 port this test just opened and closed (nothing listens)."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return "http://127.0.0.1:%d" % sock.getsockname()[1]
    finally:
        sock.close()


def api_cfg(timeout_s=1.0):
    """An api config aimed at a closed local port and never a real key source."""
    return jev_client.JevConfig(mode="api", url=closed_url(), timeout_s=timeout_s,
                                key_source="env:JEV_API_KEY")


def api_env(tmp_path):
    """Env for an api run: explicit unusable key locations and a closed url."""
    return {
        "PLANGRAPH_JEV": "api",
        "PLANGRAPH_JEV_URL": closed_url(),
        "PLANGRAPH_JEV_TIMEOUT_S": "0.2",
        "PLANGRAPH_JEV_KEY_SOURCE": "env:JEV_API_KEY",
        "JEV_API_KEY": "test-key-not-real",
        "PLANGRAPH_JEV_AUTH_PATH": str(tmp_path / "no-auth.json"),
        "PLANGRAPH_JEV_AUTH_DB": str(tmp_path / "no-auth.db"),
    }


def ok_ask(state, questions, cfg, *, env=None, opener=None):
    """A good Jev: one ``ok`` answer per question key, probabilities summing to 1."""
    out = {}
    for question_key, question in questions.items():
        options = list((question.get("criteria") or {}).keys())
        chosen = options[0]
        out[question_key] = jev_client.JevAnswer(
            status="ok", choice=chosen, probabilities={chosen: 1.0},
            confidence=1.0, reason="ok", model=cfg.model, latency_ms=1)
    return out


def slow_ask(state, questions, cfg, *, env=None, opener=None):
    time.sleep(5)
    return {}


def boom_ask(state, questions, cfg, *, env=None, opener=None):
    raise RuntimeError("boom")


class Uncopyable:
    """A value whose ``deepcopy`` raises, to exercise the verdict-copy fallback."""

    def __deepcopy__(self, memo):
        raise TypeError("cannot copy")


# -------------------------------------------- default plan_actions (no shadow)
def test_default_pass_and_blocked_pass():
    assert actions.plan_actions("pass", "P1") == PASS
    assert actions.plan_actions({"verdict": "pass"}, "P1") == PASS
    assert actions.plan_actions({"verdict": "pass", "pass_allowed": True}, "P1") == PASS
    assert actions.plan_actions(
        {"verdict": "pass", "pass_allowed": False}, "P1") == NOTHING


def test_default_fix_one_card_per_finding():
    plan = actions.plan_actions(
        {"verdict": "fix", "findings": [
            {"title": "A", "finding": "fa", "files": ["x.py"]},
            {"finding": "fb"}, "plain"]}, "P1")
    assert plan["pass"] is False and plan["inbox"] == []
    assert [card["title"] for card in plan["fix_cards"]] == [
        "A", "Review fix (P1) #2: fb", "Review fix (P1) #3: plain"]


def test_default_halt_with_and_without_key():
    plan = actions.plan_actions({"verdict": "halt", "reason": "scope",
                                 "risk_tags": ["auth"]}, "P3", key="demo:A")
    assert plan["fix_cards"] == [] and plan["pass"] is False
    assert len(plan["inbox"]) == 1
    assert plan["inbox"][0]["key"] == "demo:A"
    assert plan["inbox"][0]["default_answer"] is None
    assert plan["inbox"][0]["deadline"] is None
    with pytest.raises(ValueError):
        actions.plan_actions({"verdict": "halt", "reason": "x"}, "P3")


@pytest.mark.parametrize("verdict", [{"verdict": None}, {"verdict": "maybe"},
                                     "", None, 42])
def test_default_no_usable_word_plans_nothing(verdict):
    assert actions.plan_actions(verdict, "P6") == NOTHING


# ------------------------------------------------------- the shadow callback
def test_shadow_receives_deep_copies_and_the_plan():
    seen = []
    verdict = {"verdict": "fix", "findings": [{"finding": "f", "files": ["a.py"]}]}
    before = copy.deepcopy(verdict)

    def shadow(v, phase, key, plan):
        seen.append((v, phase, key, plan))

    plan = actions.plan_actions(verdict, "P1", key="demo:A", shadow=shadow)
    assert len(seen) == 1
    v, phase, key, hook_plan = seen[0]
    assert v == verdict and v is not verdict
    assert v["findings"] is not verdict["findings"]
    assert phase == "P1" and key == "demo:A"
    assert hook_plan == plan and hook_plan is not plan
    assert verdict == before                      # the input was left untouched


def test_mutating_in_the_callback_does_not_change_the_plan():
    verdict = {"verdict": "fix", "findings": ["f"]}
    clean = actions.plan_actions(verdict, "P1")

    def shadow(v, phase, key, plan):
        v["verdict"] = "pass"
        v["findings"].append("added")
        plan["pass"] = True
        plan["fix_cards"].append({"title": "evil"})
        plan["inbox"].append({"key": "evil"})

    got = actions.plan_actions(verdict, "P1", key="demo:A", shadow=shadow)
    assert got == clean
    assert got == {"fix_cards": [{"title": "Review fix (P1) #1: f",
                                  "finding": "f", "files": []}],
                   "inbox": [], "pass": False}
    assert verdict == {"verdict": "fix", "findings": ["f"]}


def test_a_raising_shadow_is_swallowed_and_the_plan_is_the_same():
    def shadow(*args, **kwargs):
        raise RuntimeError("shadow boom")

    assert actions.plan_actions("pass", "P1", shadow=shadow) == PASS
    assert actions.plan_actions({"verdict": "halt", "reason": "x"}, "P1",
                                key="demo:A", shadow=shadow)["pass"] is False


def test_a_halt_without_a_key_does_not_call_the_shadow():
    calls = []
    with pytest.raises(ValueError):
        actions.plan_actions({"verdict": "halt", "reason": "x"}, "P1",
                             shadow=lambda *args: calls.append(args))
    assert calls == []


def test_an_uncopyable_verdict_falls_back_to_the_original():
    verdict = {"verdict": "pass", "extra": Uncopyable()}
    seen = []
    plan = actions.plan_actions(verdict, "P1",
                                shadow=lambda v, *args: seen.append(v))
    assert plan == PASS
    assert seen == [verdict] and seen[0] is verdict


# --------------------------------------------------------------- the QC hook
def test_qc_hook_is_exported_and_never_raises():
    assert "qc_hook" in jev_shadow.__all__
    hook = jev_shadow.qc_hook(card=object(), env={})
    assert hook(object(), "P1", None, None) is None
    assert hook({"verdict": "pass"}, "P1", "", {"pass": True}) is None


def test_qc_hook_stub_records_one_row_for_a_pass():
    seen = []
    hook = jev_shadow.qc_hook(card={"title": "T"}, sink=seen.append, attempt=2,
                              env={"PLANGRAPH_JEV": "stub"})
    plan = actions.plan_actions({"verdict": "pass"}, "P1", key="demo:A", shadow=hook)
    assert plan == PASS
    assert len(seen) == 1
    row = seen[0]
    assert row["kind"] == "qc_accept"
    assert row["card"] == "demo:A" and row["attempt"] == 2
    assert row["status"] == "ok" and row["rule_answer"] == "accept"
    assert row["jev_answer"] in ("accept", "reject") and row["jev_model"] == "stub"


@pytest.mark.parametrize("verdict", [{"verdict": "fix", "reason": "bad"},
                                     {"verdict": "halt", "reason": "stop"}])
def test_qc_hook_rejects_fix_and_halt(verdict):
    seen = []
    hook = jev_shadow.qc_hook(card=None, sink=seen.append,
                              env={"PLANGRAPH_JEV": "stub"})
    actions.plan_actions(verdict, "P1", key="demo:A", shadow=hook)
    assert [row["rule_answer"] for row in seen] == ["reject"]
    assert seen[0]["card"] == "demo:A"      # the card was None: the key was added


def test_qc_hook_prefers_the_cards_own_key():
    seen = []
    hook = jev_shadow.qc_hook(card={"key": "demo:A", "title": "T"},
                              sink=seen.append, env={"PLANGRAPH_JEV": "stub"})
    actions.plan_actions({"verdict": "pass"}, "P1", key="other:B", shadow=hook)
    assert seen[0]["card"] == "demo:A"


def test_qc_hook_is_off_by_default(monkeypatch):
    calls = []

    def tracking(*args, **kwargs):
        calls.append(1)
        raise AssertionError("ask must never run while the flag is off")

    monkeypatch.setattr(jev_client, "ask", tracking)
    seen = []
    hook = jev_shadow.qc_hook(card={"key": "demo:A"}, sink=seen.append, env={})
    assert actions.plan_actions({"verdict": "pass"}, "P1", shadow=hook) == PASS
    assert seen == [] and calls == []


def test_qc_hook_timeout_is_bounded_and_leaves_the_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_client, "ask", slow_ask)
    seen = []
    hook = jev_shadow.qc_hook(card={"key": "demo:A"}, sink=seen.append,
                              cfg=api_cfg(timeout_s=0.2), env=api_env(tmp_path))
    started = time.monotonic()
    plan = actions.plan_actions({"verdict": "pass"}, "P1", shadow=hook)
    assert time.monotonic() - started < 2.0
    assert plan == PASS
    assert [row["status"] for row in seen] == ["timeout"]
    assert seen[0]["reason"].startswith("no answer within")
    assert seen[0]["jev_answer"] is None and seen[0]["rule_answer"] == "accept"


def test_qc_hook_error_status_leaves_the_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_client, "ask", boom_ask)
    seen = []
    hook = jev_shadow.qc_hook(card={"key": "demo:A"}, sink=seen.append,
                              cfg=api_cfg(), env=api_env(tmp_path))
    plan = actions.plan_actions({"verdict": "fix", "reason": "bad"}, "P1",
                               shadow=hook)
    assert plan["pass"] is False and len(plan["fix_cards"]) == 1
    assert [row["status"] for row in seen] == ["error"]
    assert seen[0]["reason"] == "RuntimeError"
    assert seen[0]["rule_answer"] == "reject"


# ----------------------------------------------------------- keeper roundtrip
def test_qc_hook_store_roundtrip(w):
    key, = seed(w, [("A", [])])
    sink = jev_shadow.store_sink(w.store, w.p("router"))
    hook = jev_shadow.qc_hook(card={"key": key, "title": "T"}, sink=sink,
                              attempt=1, env={"PLANGRAPH_JEV": "stub"})
    assert actions.plan_actions({"verdict": "pass"}, "P1", key=key,
                                shadow=hook) == PASS
    rows = w.store.jev_rows(key)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "qc_accept" and row["attempt"] == 1
    assert row["rule_answer"] == "accept" and row["status"] == "ok"
    assert row["jev_answer"] and row["jev_model"] == "stub"
    # a fix verdict records a reject beside it
    reject = jev_shadow.qc_hook(card={"key": key}, sink=sink, attempt=2,
                                env={"PLANGRAPH_JEV": "stub"})
    actions.plan_actions({"verdict": "fix", "reason": "bad"}, "P1", key=key,
                        shadow=reject)
    rows = w.store.jev_rows(key)
    assert [entry["attempt"] for entry in rows] == [1, 2]
    assert [entry["rule_answer"] for entry in rows] == ["accept", "reject"]
    assert all(entry["kind"] == "qc_accept" for entry in rows)


# -------------------------------------------------------- decision unchanged
@pytest.mark.parametrize("variant", ["off", "stub", "ok", "timeout", "error"])
def test_the_decision_is_unchanged(monkeypatch, tmp_path, variant):
    cfg = None
    env = {}
    expected = None
    if variant == "stub":
        env, expected = {"PLANGRAPH_JEV": "stub"}, "ok"
    elif variant == "ok":
        monkeypatch.setattr(jev_client, "ask", ok_ask)
        cfg, env, expected = api_cfg(), api_env(tmp_path), "ok"
    elif variant == "timeout":
        monkeypatch.setattr(jev_client, "ask", slow_ask)
        cfg, env, expected = api_cfg(timeout_s=0.2), api_env(tmp_path), "timeout"
    elif variant == "error":
        monkeypatch.setattr(jev_client, "ask", boom_ask)
        cfg, env, expected = api_cfg(), api_env(tmp_path), "error"

    cases = [({"verdict": "pass"}, "demo:A"),
             ({"verdict": "pass", "pass_allowed": False}, "demo:A"),
             ({"verdict": "fix", "findings": ["f"]}, None),
             ({"verdict": "halt", "reason": "stop"}, "demo:A")]
    for verdict, key in cases:
        plain = actions.plan_actions(verdict, "P1", key=key)
        seen = []
        hook = jev_shadow.qc_hook(card={"key": "demo:A"}, sink=seen.append,
                                  attempt=1, cfg=cfg, env=env)
        with_hook = actions.plan_actions(copy.deepcopy(verdict), "P1", key=key,
                                        shadow=hook)
        assert with_hook == plain
        assert [row["status"] for row in seen] == ([expected] if expected else [])
        if expected == "ok":
            assert seen[0]["rule_answer"] == ("accept" if plain["pass"] else "reject")
