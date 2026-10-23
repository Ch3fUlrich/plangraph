"""The report's mirrored tuples and its stub rows (RT-P1-JEV-SHADOW).

``jev_report`` copies three constants instead of importing them (it promises a
no-planner, no-schema module), so the parity tests keep them in step with
``schema``/``jev_client``.  Auto-mode placeholder answers - reason
``stub (no key)``, model ``stub`` - are counted but never scored: they stay out
of ``ok``, ``agreement_rate`` and the kappa pairs, and surface as ``stub_count``
on both public doors, the report and ``show``.
"""
from __future__ import annotations

from collections import Counter

import pytest

from jev_util import seed_jev_rows, stub_ask
from keeper_util import World, seed
from plangraph.keeper import fmt, jev_report, schema
from plangraph.planner import jev_client


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


# --------------------------------------------------------------- parity
def test_report_kind_and_status_tuples_match_the_schema():
    assert jev_report._KINDS == schema.JEV_KINDS
    assert jev_report._STATUSES == schema.JEV_STATUSES


def test_stub_marker_matches_the_client_auto_fallback():
    assert jev_report._STUB_REASON == jev_client.STUB_FALLBACK_REASON
    assert jev_client.STUB_FALLBACK_REASON == "stub (no key)"


# ------------------------------------------------------------ stub rows
def test_stub_answers_are_counted_but_never_scored(w, monkeypatch):
    real_key, stub_key = seed(w, [("A", []), ("B", [])])
    seed_jev_rows(w, real_key, monkeypatch)            # a real answer
    seed_jev_rows(w, stub_key, monkeypatch,
                  ask=stub_ask("reject"))              # placeholder, no key

    router = w.p("router")
    real = w.store.jev_rows(real_key)
    stubs = w.store.jev_rows(stub_key)
    rows, marker = real + stubs, jev_client.STUB_FALLBACK_REASON
    assert real and stubs
    assert all(r["reason"] == marker and r["jev_model"] == "stub"
               for r in stubs)

    report = w.store.jev_report(router)
    overall = report["overall"]
    assert (overall["total"], overall["stub_count"]) == (len(rows), len(stubs))
    assert overall["ok"] == len(real)                  # stubs never reach ok
    assert overall["agreement_rate"] == round(overall["agree"]
                                              / overall["ok"], 4)
    assert overall["agree"] + overall["disagree"] == len(real)

    # the kappa pairs come from the real rows alone: the confusion matrix is
    # exactly what those rows make, not doubled by the placeholders
    pairs = Counter((str(r["rule_answer"]), str(r["jev_answer"]))
                    for r in real)
    labels = sorted({word for pair in pairs for word in pair})
    assert overall["labels"] == labels
    assert overall["confusion"] == {a: {b: pairs.get((a, b), 0)
                                        for b in labels} for a in labels}

    text = fmt.jev_report_text(report)                 # the report door
    assert f"stubs {len(stubs)}" in text

    info = w.store.show(router, stub_key)              # the show door
    assert info["jev_agreement"]["stub_count"] == len(stubs)
    assert info["jev_agreement"]["ok"] == 0
    assert [v["agree"] for v in info["jev_shadow"]] == [None] * len(stubs)
    assert f"stubs {len(stubs)}" in fmt.show_text(info)
