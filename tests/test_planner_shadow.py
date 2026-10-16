"""Planner shadow hook (D18): audit-only shadow answerer, never the decision (hermetic).

Every answerer here is a plain fake function: no model calls, no network, no store.
"""
from __future__ import annotations

import copy

import pytest

from plangraph.planner import routing
from plangraph.planner.shadow import (ENV_FLAG, ENV_LOG, enabled, read_rows,
                                      record_outcome, route_with_shadow, summarize)


def card(**kw):
    base = {"outcome": "do it", "estimate_tokens": 40000, "risk_tags": [],
            "window_class": None}
    base.update(kw)
    return base


def agree_answer(card_, route):
    return {"legs": [route.legs[0].id], "window_class": route.window_class}


def disagree_answer(card_, route):
    return {"legs": ["never-a-real-leg"], "window_class": "1m"}


def rules_of(card_):
    return routing.route_card(card_)


# --- flag --------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " On "])
def test_enabled_accepts_only_the_on_values(value):
    assert enabled({ENV_FLAG: value}) is True


@pytest.mark.parametrize("value", ["", "0", "off", "no", "maybe", None])
def test_enabled_default_off(value):
    assert enabled({ENV_FLAG: value} if value is not None else {}) is False


def test_enabled_reads_os_environ_by_default(monkeypatch):
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert enabled() is False
    monkeypatch.setenv(ENV_FLAG, "1")
    assert enabled() is True


# --- routing stays the rules' decision ---------------------------------------------

def test_off_by_default_writes_nothing_and_returns_the_rules_route(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_FLAG, raising=False)
    monkeypatch.delenv(ENV_LOG, raising=False)
    log = tmp_path / "shadow.jsonl"
    c = card(key="A")
    got, did = route_with_shadow(c, answerer=agree_answer, log_path=str(log))
    assert did is None
    assert got == rules_of(c)
    assert not log.exists()


def test_enabled_without_answerer_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_LOG, raising=False)
    log = tmp_path / "shadow.jsonl"
    c = card(key="A")
    got, did = route_with_shadow(c, env={ENV_FLAG: "1"}, log_path=str(log))
    assert did is None
    assert got == rules_of(c)
    assert not log.exists()


def test_enabled_with_no_resolved_log_path_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_LOG, raising=False)
    c = card(key="A")
    got, did = route_with_shadow(c, answerer=agree_answer, env={ENV_FLAG: "1"})
    assert (got, did) == (rules_of(c), None)
    assert list(tmp_path.iterdir()) == []


def test_shadow_disagreeing_never_alters_the_returned_route(tmp_path):
    log = tmp_path / "shadow.jsonl"
    c = card(key="A")
    got, did = route_with_shadow(c, answerer=disagree_answer, log_path=str(log),
                                 env={ENV_FLAG: "1"})
    assert got == rules_of(c)
    row = read_rows(str(log))[0]
    assert row["id"] == did
    assert row["agree"] is False
    assert row["shadow"]["legs"] == ["never-a-real-leg"]


# --- the audit row -----------------------------------------------------------------

def test_env_on_and_fake_answerer_write_one_decision_row(tmp_path):
    log = tmp_path / "shadow.jsonl"
    c = card(key="A")
    got, did = route_with_shadow(c, answerer=agree_answer, log_path=str(log),
                                 env={ENV_FLAG: "ON"})
    rules = rules_of(c)
    assert did is not None and len(did) == 32
    assert got == rules
    rows = read_rows(str(log))
    assert len(rows) == 1
    row = rows[0]
    assert row["v"] == 1
    assert row["event"] == "decision"
    assert row["id"] == did
    assert row["card_key"] == "A"
    assert row["ts"].endswith("Z")
    assert row["rules"] == {"window_class": rules.window_class,
                            "legs": [leg.id for leg in rules.legs],
                            "review_seats": rules.review_seats,
                            "reasons": list(rules.reasons)}
    assert row["shadow"] == {"legs": [rules.legs[0].id],
                             "window_class": rules.window_class}
    assert row["shadow_error"] is None
    assert row["agree"] is True


def test_flag_and_log_path_resolve_from_the_environment(tmp_path, monkeypatch):
    log = tmp_path / "shadow.jsonl"
    monkeypatch.setenv(ENV_FLAG, "yes")
    monkeypatch.setenv(ENV_LOG, str(log))
    c = card(key="A")
    got, did = route_with_shadow(c, answerer=agree_answer)
    assert got == rules_of(c)
    rows = read_rows(str(log))
    assert [r["event"] for r in rows] == ["decision"]
    assert rows[0]["id"] == did


def test_answerer_failure_is_logged_and_ignored(tmp_path):
    log = tmp_path / "shadow.jsonl"
    c = card(key="A")
    rules = rules_of(c)

    def boom(card_, route):
        raise RuntimeError("kaboom " + "x" * 900)

    got, did = route_with_shadow(c, answerer=boom, log_path=str(log),
                                 env={ENV_FLAG: "1"})
    assert got == rules
    rows = read_rows(str(log))
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == did
    assert row["shadow"] is None
    assert row["agree"] is None
    assert row["shadow_error"].startswith("RuntimeError: kaboom")
    assert row["shadow_error"] == "RuntimeError: " + ("kaboom " + "x" * 900)[:200]


def test_answerer_mutation_does_not_leak_to_the_caller(tmp_path):
    log = tmp_path / "shadow.jsonl"
    c = card(key="A", risk_tags=[])
    before = copy.deepcopy(c)
    seen = []

    def mutate(card_, route):
        card_["outcome"] = "clobbered"
        card_["risk_tags"].append("money")
        card_["heldout_check"] = "leaked"
        seen.append(card_)
        return {"legs": [route.legs[0].id]}

    route_with_shadow(c, answerer=mutate, log_path=str(log), env={ENV_FLAG: "1"})
    assert c == before
    assert seen and seen[0] is not c
    # the leaked field the answerer wrote into its copy is redacted from the row
    assert "leaked" not in log.read_text()


def test_unwritable_log_path_is_swallowed(tmp_path):
    blocked = tmp_path / "as-dir"
    blocked.mkdir()
    c = card(key="A")
    got, did = route_with_shadow(c, answerer=agree_answer, log_path=str(blocked),
                                 env={ENV_FLAG: "1"})
    assert got == rules_of(c)
    assert did is not None
    assert record_outcome(did, {"exit_class": "ok"}, log_path=str(blocked),
                          env={ENV_FLAG: "1"}) is False


# --- outcomes and summary ----------------------------------------------------------

def test_record_outcome_appends_outcome_joined_by_summarize(tmp_path):
    log = tmp_path / "shadow.jsonl"
    c = card(key="A")
    got, did = route_with_shadow(c, answerer=agree_answer, log_path=str(log),
                                 env={ENV_FLAG: "1"})
    written = record_outcome(did, {"exit_class": "ok", "leg": got.legs[0].id},
                             log_path=str(log), env={ENV_FLAG: "1"})
    assert written is True
    rows = read_rows(str(log))
    assert [r["event"] for r in rows] == ["decision", "outcome"]
    out = rows[1]
    assert out["v"] == 1
    assert out["id"] == did
    assert out["ts"].endswith("Z")
    assert out["outcome"] == {"exit_class": "ok", "leg": got.legs[0].id}
    assert summarize(rows) == {"decisions": 1, "outcomes": 1, "agree": 1,
                               "disagree": 0, "shadow_errors": 0}


def test_record_outcome_false_when_disabled_or_id_missing(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_FLAG, raising=False)
    monkeypatch.delenv(ENV_LOG, raising=False)
    log = tmp_path / "shadow.jsonl"
    assert record_outcome("deadbeef", {"exit_class": "ok"},
                          log_path=str(log)) is False
    assert record_outcome(None, {"exit_class": "ok"}, log_path=str(log),
                          env={ENV_FLAG: "1"}) is False
    assert record_outcome("deadbeef", {"exit_class": "ok"},
                          env={ENV_FLAG: "1"}) is False
    assert not log.exists()


def test_summarize_joins_counts_across_rows(tmp_path):
    log = tmp_path / "shadow.jsonl"
    for answerer in (agree_answer, disagree_answer):
        _, did = route_with_shadow(card(key="A"), answerer=answerer,
                                   log_path=str(log), env={ENV_FLAG: "1"})
        assert record_outcome(did, {"exit_class": "ok"}, log_path=str(log),
                              env={ENV_FLAG: "1"}) is True

    def boom(card_, route):
        raise ValueError("nope")

    route_with_shadow(card(key="A"), answerer=boom, log_path=str(log),
                      env={ENV_FLAG: "1"})
    rows = read_rows(str(log))
    assert len(rows) == 5
    assert summarize(rows) == {"decisions": 3, "outcomes": 2, "agree": 1,
                               "disagree": 1, "shadow_errors": 1}


# --- redaction and the tolerant reader --------------------------------------------

def test_heldout_check_and_card_text_never_reach_the_log(tmp_path):
    log = tmp_path / "shadow.jsonl"
    c = card(key="A", heldout_check="HELDOUT-MARKER expect 42",
             check_cmd="pytest -q tests/test_a.py", write_set=["src/a.py"])

    def echo_card(card_, route):
        return {"legs": [route.legs[0].id], "echo": dict(card_)}

    route_with_shadow(c, answerer=echo_card, log_path=str(log), env={ENV_FLAG: "1"})
    text = log.read_text()
    assert text.strip()
    for secret in ("HELDOUT-MARKER", "heldout_check", "check_cmd", "write_set",
                   "pytest -q", "src/a.py"):
        assert secret not in text
    row = read_rows(str(log))[0]
    assert row["card_key"] == "A"
    shadow = row["shadow"]
    assert shadow["legs"] == [rules_of(c).legs[0].id]
    assert shadow["echo"]["outcome"] == "do it"
    for secret in ("heldout_check", "check_cmd", "write_set"):
        assert secret not in shadow["echo"]


def test_read_rows_skips_bad_lines_and_missing_files(tmp_path):
    log = tmp_path / "shadow.jsonl"
    log.write_text('{"v": 1, "event": "decision"}\n\nnot json at all\n'
                   '"just a string"\n[1, 2]\n{"v": 2, "event": "outcome"}\n')
    rows = read_rows(str(log))
    assert [r["v"] for r in rows] == [1, 2]
    assert read_rows(tmp_path / "missing.jsonl") == []
