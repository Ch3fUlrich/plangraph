"""Hermetic doubles for the Jev shadow tests (RT-P1-JEV-SHADOW).

No network, no key and no model: the client's ``ask`` is replaced by the
doubles below and Jev's own switch is turned on with ``monkeypatch.setenv``.
``seed_jev_rows`` writes rows through the production routing hook with its
real ``store``/``principal`` wiring (the way the planner calls it), so no test
injects a store seam into ``plan_actions`` or ``record_outcome``.
"""
from __future__ import annotations

import time
from dataclasses import replace

from plangraph.planner import jev_client, shadow as shadow_mod


def fixed_ask(choice):
    """A fake Jev that answers ``choice`` for every question it is asked."""
    def ask(state, questions, cfg, *, env=None, opener=None):
        answers = {}
        for key, question in questions.items():
            options = list((question.get("criteria") or {}).keys())
            chosen = choice if choice in options else options[0]
            answers[key] = jev_client.JevAnswer(
                status="ok", choice=chosen, probabilities={chosen: 1.0},
                confidence=1.0, reason="ok", model=cfg.model, latency_ms=1)
        return answers
    return ask


def stub_ask(choice):
    """A fake Jev wearing the ``auto`` no-key placeholder markers."""
    good = fixed_ask(choice)

    def ask(state, questions, cfg, *, env=None, opener=None):
        answers = good(state, questions, cfg, env=env, opener=opener)
        return {key: replace(answer, reason=jev_client.STUB_FALLBACK_REASON,
                             model="stub")
                for key, answer in answers.items()}
    return ask


def slow_ask(state, questions, cfg, *, env=None, opener=None):
    """A Jev that never answers in time (the guard turns it into a timeout)."""
    time.sleep(5)
    return {}


def unavailable_ask(state, questions, cfg, *, env=None, opener=None):
    """A Jev that is reachable but has no answer to give."""
    return {key: jev_client.JevAnswer(status="unavailable",
                                      reason="no answer",
                                      model=cfg.model, latency_ms=2)
            for key in questions}


def jev_on(monkeypatch, ask=None):
    """Turn Jev on (mode ``api``) and hand the client's call to *ask*."""
    monkeypatch.setattr(jev_client, "ask", ask or fixed_ask("accept"))
    monkeypatch.setenv("PLANGRAPH_JEV", "api")


def ask_recorder():
    """``(calls, ask)``: records every call and fails if it ever runs."""
    calls = []

    def ask(state, questions, cfg, *, env=None, opener=None):
        calls.append(1)
        raise AssertionError("ask must never run while Jev is off")

    return calls, ask


def card(**kw):
    """A minimal routable card for the routing hook; ``key`` names the store card."""
    base = {"key": "demo:A", "outcome": "do it", "estimate_tokens": 40000,
            "risk_tags": [], "window_class": None}
    base.update(kw)
    return base


def seed_jev_rows(w, key, monkeypatch, attempt=None, ask=None):
    """Write one routing run's rows for *key* via ``route_with_shadow``."""
    jev_on(monkeypatch, ask)
    shadow_mod.route_with_shadow(card(key=key), store=w.store,
                                 principal=w.p("router"), attempt=attempt)
    from plangraph.planner.jev_shadow import drain
    drain()
    return w.store.jev_rows(key)
