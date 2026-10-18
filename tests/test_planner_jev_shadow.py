"""Tests for the Jev shadow runner (RT-P1-JEV-SHADOW): hermetic, no model, no network.

Jev is a free decision model run in SHADOW: it never changes a decision, a slow or
failing Jev never blocks, and the flag defaults to ``off``.  These tests drive the
runner's real entry points (``shadow_routing`` / ``shadow_qc_accept``) with stub
mode and fake clients; the two ``api``-mode tests point ``cfg.url`` at a closed
127.0.0.1 port and never let a default credential location be read (the auth path/db
overrides are set to unusable tmp paths).
"""
from __future__ import annotations

import copy
import dataclasses
import json
import logging
import socket
import time

import pytest

from keeper_util import World, seed
from plangraph.planner import jev_client, jev_questions, jev_shadow, routing


# --------------------------------------------------------------------- helpers
def card(**kw):
    """A routable card (key defaults to a real-looking keeper key)."""
    base = {"key": "demo:A", "outcome": "do it", "estimate_tokens": 40000,
            "risk_tags": [], "window_class": None}
    base.update(kw)
    return base


def route_for(c=None):
    return routing.route_card(c if c is not None else card())


def stub_cfg(timeout_s=1.0):
    return jev_client.JevConfig(mode="stub", timeout_s=timeout_s)


def api_cfg(url="http://127.0.0.1:1", timeout_s=1.0):
    return jev_client.JevConfig(mode="api", url=url, timeout_s=timeout_s,
                                key_source="env:JEV_API_KEY")


def ok_ask(state, questions, cfg, *, env=None, opener=None):
    """A good Jev: one ``ok`` answer per question key, probabilities summing to 1."""
    out = {}
    for key, question in questions.items():
        options = list((question.get("criteria") or {}).keys())
        chosen = options[0]
        out[key] = jev_client.JevAnswer(status="ok", choice=chosen,
                                        probabilities={chosen: 1.0},
                                        confidence=1.0, reason="ok",
                                        model=cfg.model, latency_ms=1)
    return out


def boom_ask(state, questions, cfg, *, env=None, opener=None):
    raise RuntimeError("boom")


def slow_ask(state, questions, cfg, *, env=None, opener=None):
    time.sleep(5)
    return {}


def unavailable_ask(state, questions, cfg, *, env=None, opener=None):
    return {key: jev_client.JevAnswer(status="unavailable", reason="http 503",
                                      model=cfg.model, latency_ms=2)
            for key in questions}


def raise_error():
    raise RuntimeError("x")


def closed_url():
    """A URL on a 127.0.0.1 port this test just opened and closed (nothing listens)."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return "http://127.0.0.1:%d" % sock.getsockname()[1]
    finally:
        sock.close()


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


# ------------------------------------------------------------------------ off
def test_off_does_no_work(monkeypatch):
    monkeypatch.delenv("PLANGRAPH_JEV", raising=False)
    calls = []
    seen = []

    def tracking(*args, **kwargs):
        calls.append(1)
        raise AssertionError("ask must never run while the flag is off")

    monkeypatch.setattr(jev_client, "ask", tracking)
    c = card()
    route = route_for(c)
    sink = seen.append
    # env={} -> mode off; and an explicit off cfg
    assert jev_shadow.shadow_routing(c, route, sink=sink, env={}) == []
    assert jev_shadow.shadow_qc_accept(c, {"verdict": "pass"}, sink=sink, env={}) is None
    off = jev_client.JevConfig(mode="off")
    assert jev_shadow.shadow_routing(c, route, sink=sink, cfg=off) == []
    assert jev_shadow.shadow_qc_accept(c, {"verdict": "pass"}, sink=sink, cfg=off) is None
    assert calls == []
    assert seen == []


# ----------------------------------------------------------------- stub run
def test_stub_routing_rows_in_order_and_sink():
    cfg = stub_cfg()
    c = card()
    route = route_for(c)
    seen = []
    rows = jev_shadow.shadow_routing(c, route, sink=seen.append, attempt=1,
                                     cfg=cfg, env={})
    assert [r["kind"] for r in rows] == list(jev_questions.ROUTING_KINDS)
    assert seen == rows                     # the sink received exactly these rows
    assert {r["kind"]: r["rule_answer"] for r in rows} == jev_questions.rule_answers(route)
    for row in rows:
        assert row["card"] == "demo:A" and row["attempt"] == 1
        assert row["status"] == "ok"
        assert isinstance(row["jev_answer"], str) and row["jev_answer"]
        assert isinstance(row["jev_probabilities"], dict)
        assert abs(sum(row["jev_probabilities"].values()) - 1.0) < 1e-9
        assert row["jev_confidence"] is not None
        assert row["jev_model"] == "stub"
        assert row["latency_ms"] == 0
        assert set(row) == {"card", "attempt", "kind", "rule_answer", "jev_answer",
                            "jev_probabilities", "jev_confidence", "status",
                            "reason", "jev_model", "latency_ms"}


# -------------------------------------------------------- decision unchanged
@pytest.mark.parametrize("variant", ["off", "stub", "ok", "timeout", "error",
                                     "unavailable"])
def test_decision_is_unchanged(monkeypatch, variant):
    monkeypatch.delenv("PLANGRAPH_JEV", raising=False)
    c = card(risk_tags=["auth"], title="T", subtasks=[{"id": "s1"}],
             write_set=["a.py", "b.py"])
    route = route_for(c)
    route_snapshot = copy.deepcopy(route)
    route_dict = dataclasses.asdict(route)
    route_before = routing.route_card(c)
    card_snapshot = copy.deepcopy(c)

    cfg = jev_client.JevConfig(mode="off")
    if variant == "stub":
        cfg = stub_cfg()
    elif variant == "ok":
        monkeypatch.setattr(jev_client, "ask", ok_ask)
        cfg = api_cfg()
    elif variant == "timeout":
        monkeypatch.setattr(jev_client, "ask", slow_ask)
        cfg = api_cfg(timeout_s=0.2)
    elif variant == "error":
        monkeypatch.setattr(jev_client, "ask", boom_ask)
        cfg = api_cfg()
    elif variant == "unavailable":
        monkeypatch.setattr(jev_client, "ask", unavailable_ask)
        cfg = api_cfg()

    out = jev_shadow.shadow_routing(c, route, cfg=cfg, env={})

    # only rows come back - never a route
    assert isinstance(out, list)
    assert all(isinstance(r, dict) for r in out)
    assert not any("legs" in r for r in out)
    # nothing the runner touched changed
    assert route == route_snapshot
    assert dataclasses.asdict(route) == route_dict
    assert route == route_before
    assert c == card_snapshot
    if variant == "off":
        assert out == []
    else:
        assert [r["kind"] for r in out] == list(jev_questions.ROUTING_KINDS)


# ------------------------------------------------------------------- guarded
def test_guarded_ok_timeout_and_error():
    assert jev_shadow.guarded(lambda: 5, 1.0) == ("ok", 5, "")
    started = time.monotonic()
    status, value, reason = jev_shadow.guarded(lambda: time.sleep(5), 0.1)
    assert status == "timeout" and value is None
    assert reason == "no answer within 0.1s"
    assert time.monotonic() - started < 2.0
    assert jev_shadow.guarded(raise_error, 1.0) == ("error", None, "RuntimeError")


def test_a_non_positive_timeout_is_a_tiny_positive_wait():
    # `join(0)` would report an immediate timeout; the contract is a tiny positive
    # value instead, so the wait used is deterministic regardless of thread startup.
    assert jev_shadow._positive_wait(0) == 0.001
    assert jev_shadow._positive_wait(-5) == 0.001
    assert jev_shadow._positive_wait(None) == 0.001
    assert jev_shadow._positive_wait(float("inf")) == 0.001
    assert jev_shadow._positive_wait(0.2) == 0.2


def test_timeout_is_bounded_and_reported_for_every_kind(monkeypatch):
    monkeypatch.setattr(jev_client, "ask", slow_ask)
    c = card()
    route = route_for(c)
    started = time.monotonic()
    rows = jev_shadow.shadow_routing(c, route, cfg=api_cfg(timeout_s=0.2), env={})
    assert time.monotonic() - started < 2.0
    assert [r["status"] for r in rows] == ["timeout"] * 4
    assert all(r["reason"].startswith("no answer within") for r in rows)
    assert all(r["jev_answer"] is None for r in rows)


def test_error_is_swallowed_without_the_message(monkeypatch):
    monkeypatch.setattr(jev_client, "ask", boom_ask)
    c = card()
    rows = jev_shadow.shadow_routing(c, route_for(c), cfg=api_cfg(), env={})
    assert [r["status"] for r in rows] == ["error"] * 4
    assert all(r["reason"] == "RuntimeError" for r in rows)
    assert "boom" not in json.dumps(rows)


def test_unavailable_answers_become_rows(monkeypatch):
    monkeypatch.setattr(jev_client, "ask", unavailable_ask)
    c = card()
    rows = jev_shadow.shadow_routing(c, route_for(c), cfg=api_cfg(), env={})
    assert [r["status"] for r in rows] == ["unavailable"] * 4
    assert all(r["jev_answer"] is None for r in rows)


def test_a_raising_sink_is_swallowed():
    def bad_sink(row):
        raise RuntimeError("sink boom")

    c = card()
    rows = jev_shadow.shadow_routing(c, route_for(c), sink=bad_sink, cfg=stub_cfg(),
                                     env={})
    assert len(rows) == 4


def test_missing_card_key_returns_rows_but_skips_the_sink():
    seen = []
    for key in (None, ""):
        c = card(key=key)
        rows = jev_shadow.shadow_routing(c, route_for(c), sink=seen.append,
                                         cfg=stub_cfg(), env={})
        assert len(rows) == 4
        assert all(r["card"] == key for r in rows)
    assert seen == []


def test_the_hooks_swallow_an_internal_error(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(jev_questions, "routing_questions", boom)
    assert jev_shadow.shadow_routing(card(), route_for(), cfg=stub_cfg(), env={}) == []
    monkeypatch.setattr(jev_questions, "qc_question", boom)
    assert jev_shadow.shadow_qc_accept(card(), {"verdict": "pass"},
                                       cfg=stub_cfg(), env={}) is None


# ---------------------------------------------------------------- QC-accept
def test_qc_off_returns_none(monkeypatch):
    monkeypatch.delenv("PLANGRAPH_JEV", raising=False)
    assert jev_shadow.shadow_qc_accept(card(), {"verdict": "pass"}, env={}) is None
    assert jev_shadow.shadow_qc_accept(
        card(), {"verdict": "pass"}, cfg=jev_client.JevConfig(mode="off")) is None


def test_qc_stub_row_and_rule_answer():
    seen = []
    row = jev_shadow.shadow_qc_accept(card(), {"verdict": "pass"}, sink=seen.append,
                                      attempt=1, cfg=stub_cfg(), env={})
    assert row["kind"] == "qc_accept"
    assert row["card"] == "demo:A" and row["attempt"] == 1
    assert row["status"] == "ok" and row["jev_model"] == "stub"
    assert row["rule_answer"] == "accept"
    assert seen == [row]


def test_qc_rule_answer_reads_bool_then_verdict():
    cfg = stub_cfg()
    assert jev_shadow.shadow_qc_accept(card(), None, rule_accept=True,
                                       cfg=cfg)["rule_answer"] == "accept"
    assert jev_shadow.shadow_qc_accept(card(), None, rule_accept=False,
                                       cfg=cfg)["rule_answer"] == "reject"
    assert jev_shadow.shadow_qc_accept(card(), {"verdict": "pass"},
                                       cfg=cfg)["rule_answer"] == "accept"
    for verdict in ({"verdict": "fix"}, {"verdict": "halt"}, None, "garbage"):
        assert jev_shadow.shadow_qc_accept(card(), verdict, cfg=cfg)["rule_answer"] == "reject"


def test_qc_never_raises_on_odd_input():
    cfg = stub_cfg()
    assert jev_shadow.shadow_qc_accept(card(), object(), cfg=cfg)["kind"] == "qc_accept"
    row = jev_shadow.shadow_qc_accept(None, object(), cfg=cfg)
    assert row["kind"] == "qc_accept" and row["card"] is None


def test_qc_timeout_and_error_rows(monkeypatch):
    monkeypatch.setattr(jev_client, "ask", slow_ask)
    row = jev_shadow.shadow_qc_accept(card(), {"verdict": "pass"},
                                      cfg=api_cfg(timeout_s=0.2), env={})
    assert row["status"] == "timeout" and row["kind"] == "qc_accept"
    monkeypatch.setattr(jev_client, "ask", boom_ask)
    row = jev_shadow.shadow_qc_accept(card(), {"verdict": "pass"},
                                      cfg=api_cfg(), env={})
    assert row["status"] == "error" and row["reason"] == "RuntimeError"


# ----------------------------------------------------------- keeper roundtrip
def test_store_sink_roundtrip(w):
    key, = seed(w, [("A", [])])
    c = card(key=key)
    sink = jev_shadow.store_sink(w.store, w.p("router"))
    rows = jev_shadow.shadow_routing(c, route_for(c), sink=sink, attempt=1,
                                     cfg=stub_cfg(), env={})
    stored = w.store.jev_rows(key)
    assert len(stored) == 4
    assert [entry["kind"] for entry in stored] == list(jev_questions.ROUTING_KINDS)
    for row, entry in zip(rows, stored):
        assert entry["key"] == key and entry["attempt"] == 1
        assert entry["rule_answer"] == row["rule_answer"]
        assert entry["jev_answer"] == row["jev_answer"]
        assert entry["status"] == "ok" and entry["jev_model"] == "stub"
        assert entry["outcome"] is None


def test_store_sink_refusing_principal_returns_false(w):
    key, = seed(w, [("A", [])])
    c = card(key=key)
    sink = jev_shadow.store_sink(w.store, w.p("reader"))
    rows = jev_shadow.shadow_routing(c, route_for(c), sink=sink, cfg=stub_cfg(),
                                     env={})
    assert len(rows) == 4
    assert sink(rows[0]) is False
    assert w.store.jev_rows(key) == []


def test_fill_outcome_fills_once_and_by_attempt(w):
    key, = seed(w, [("A", [])])
    c = card(key=key)
    jev_shadow.shadow_routing(c, route_for(c),
                              sink=jev_shadow.store_sink(w.store, w.p("router")),
                              attempt=2, cfg=stub_cfg(), env={})
    assert jev_shadow.fill_outcome(w.store, w.p("router"), key, "merged",
                                   attempt=2) == 4
    assert jev_shadow.fill_outcome(w.store, w.p("router"), key, "merged",
                                   attempt=2) == 0
    entries = w.store.jev_rows(key)
    assert [entry["outcome"] for entry in entries] == ["merged"] * 4
    assert all(entry["outcome_at"] is not None for entry in entries)


def test_fill_outcome_without_attempt_fills_every_attempt(w):
    key, = seed(w, [("A", [])])
    c = card(key=key)
    jev_shadow.shadow_routing(c, route_for(c),
                              sink=jev_shadow.store_sink(w.store, w.p("router")),
                              attempt=3, cfg=stub_cfg(), env={})
    assert jev_shadow.fill_outcome(w.store, w.p("router"), key, "any") == 4


def test_fill_outcome_swallows_a_raising_store():
    class Boom:
        def jev_outcome(self, *args, **kwargs):
            raise RuntimeError("nope")

    assert jev_shadow.fill_outcome(Boom(), object(), "demo:A", "x") == 0


# ------------------------------------------------------------------ jsonl sink
def test_jsonl_sink_writes_one_parsable_line_per_row(tmp_path):
    path = tmp_path / "jev.jsonl"
    sink = jev_shadow.jsonl_sink(path)
    rows = [{"card": "demo:A", "kind": "harness", "status": "ok"},
            {"card": "demo:A", "kind": "window", "status": "timeout"}]
    assert [sink(row) for row in rows] == [True, True]
    lines = [json.loads(line) for line in
             path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    for line, row in zip(lines, rows):
        assert line["v"] == 1 and line["event"] == "jev"
        assert isinstance(line["ts"], str) and line["ts"].endswith("Z")
        assert line["kind"] == row["kind"]


def test_jsonl_sink_tolerates_an_unwritable_path(tmp_path):
    assert jev_shadow.jsonl_sink(None)({"card": "demo:A"}) is False
    assert jev_shadow.jsonl_sink("")({"card": "demo:A"}) is False
    assert jev_shadow.jsonl_sink(tmp_path)({"card": "demo:A"}) is False


# --------------------------------------------------------------- key hygiene
def test_the_key_never_reaches_a_row_or_the_log(tmp_path, caplog):
    fake_key = "test-key-not-real"
    env = {
        "PLANGRAPH_JEV_KEY_SOURCE": "env:JEV_API_KEY",
        "JEV_API_KEY": fake_key,
        "PLANGRAPH_JEV_AUTH_PATH": str(tmp_path / "no-auth.json"),
        "PLANGRAPH_JEV_AUTH_DB": str(tmp_path / "no-auth.db"),
    }
    cfg = api_cfg(url=closed_url(), timeout_s=0.5)
    c = card()
    with caplog.at_level(logging.DEBUG, logger="plangraph"):
        rows = jev_shadow.shadow_routing(c, route_for(c), cfg=cfg, env=env)
    assert [r["status"] for r in rows] == ["unavailable"] * 4
    assert fake_key not in json.dumps(rows)
    assert fake_key not in repr(cfg)
    assert fake_key not in caplog.text
