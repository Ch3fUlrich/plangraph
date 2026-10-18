"""Jev question builders (RT-P1-JEV-SHADOW): hermetic tests (no model, no network).

Cards are plain dicts routed with the real ``routing.route_card``; verdicts are plain
strings, dicts and attribute objects.  Nothing here touches the store or Jev itself.
"""
from __future__ import annotations

from plangraph.keeper.schema import WINDOW_CLASSES
from plangraph.planner import jev_questions as jq
from plangraph.planner import routing
from plangraph.planner.legs import HARNESSES


def card(**kw):
    base = {"outcome": "do it", "estimate_tokens": 40000, "risk_tags": [],
            "window_class": None}
    base.update(kw)
    return base


# --- questions match the real enums ------------------------------------------------

def test_routing_questions_are_keyed_by_kind():
    questions = jq.routing_questions(card())
    assert tuple(questions) == jq.ROUTING_KINDS


def test_harness_options_are_the_real_enum_in_order():
    q = jq.routing_questions(card())["harness"]
    assert q["type"] == "choice"
    assert list(q["criteria"]) == list(HARNESSES)


def test_window_options_are_the_real_enum_in_order():
    q = jq.routing_questions()["window"]
    assert list(q["criteria"]) == list(WINDOW_CLASSES)


def test_budget_options_cover_the_rule_default():
    q = jq.routing_questions()["budget"]
    assert list(q["criteria"]) == list(jq.BUDGET_OPTIONS)
    assert str(routing.ATTEMPT_BUDGET) in jq.BUDGET_OPTIONS


def test_review_levels_are_exactly_what_routing_can_produce():
    untagged = routing.route_card(card())
    tagged = routing.route_card(card(risk_tags=["auth"]))
    assert untagged.review_level == "standard"
    assert tagged.review_level == "risky"
    assert set(jq.REVIEW_LEVELS) == {untagged.review_level, tagged.review_level}


def test_every_question_has_one_line_instructions_and_descriptions():
    for question in jq.routing_questions(card()).values():
        assert isinstance(question["instructions"], str) and question["instructions"]
        assert "\n" not in question["instructions"]
        assert question["criteria"]
        for description in question["criteria"].values():
            assert isinstance(description, str) and description.strip()
            assert "\n" not in description


# --- validation --------------------------------------------------------------------

def test_validate_accepts_builder_output():
    assert jq.validate_questions(jq.routing_questions(card())) == []


def test_validate_rejects_malformed_questions():
    valid = jq.routing_questions()["harness"]
    assert jq.validate_questions({"harness": valid}) == []
    assert jq.validate_questions({}) != []
    assert jq.validate_questions({"x": 5}) != []
    assert jq.validate_questions({"x": {"type": "choice", "instructions": "hi"}}) != []
    assert jq.validate_questions(
        {"x": {"type": "choice", "instructions": "hi", "criteria": {}}}) != []
    assert jq.validate_questions(
        {"x": {"type": "rank", "instructions": "hi", "criteria": {"a": "b"}}}) != []
    assert jq.validate_questions(
        {"x": {"type": "choice", "instructions": "", "criteria": {"a": "b"}}}) != []
    many = {f"option-{i}": "desc" for i in range(300)}
    assert jq.validate_questions(
        {"x": {"type": "choice", "instructions": "hi", "criteria": many}}) != []


# --- rule answers ------------------------------------------------------------------

def test_rule_answers_match_the_untagged_route():
    route = routing.route_card(card())
    assert jq.rule_answers(route) == {
        "harness": route.harness, "window": route.window_class,
        "budget": str(route.attempt_budget), "review": route.review_level}


def test_rule_answers_match_the_tagged_route():
    route = routing.route_card(card(risk_tags=["auth"]))
    answers = jq.rule_answers(route)
    assert answers["review"] == "risky"
    assert answers["harness"] == route.harness
    assert answers["window"] == route.window_class


def test_rule_answers_match_a_window_pinned_route():
    route = routing.route_card(card(window_class="256k"))
    assert route.window_class == "256k"
    assert jq.rule_answers(route)["window"] == "256k"


# --- routing state -----------------------------------------------------------------

def test_routing_state_never_leaks_secret_card_fields():
    c = card(key="A", heldout_check="SECRET-HELDOUT-MARKER expect 42",
             check_cmd="SECRET-CHECK-MARKER pytest -q",
             write_set=["SECRET-WRITE-MARKER/src/a.py"])
    route = routing.route_card(c)
    state = jq.routing_state(c, route)
    for secret in ("SECRET-HELDOUT-MARKER", "SECRET-CHECK-MARKER",
                   "SECRET-WRITE-MARKER", "SECRET-WRITE-MARKER/src/a.py",
                   "heldout_check", "check_cmd", "decision:"):
        assert secret not in state
    assert "review_level" not in state
    assert "attempt_budget" not in state


def test_routing_state_carries_the_inputs_jev_needs():
    c = card(key="A", risk_tags=["auth"], title="T", write_set=["a.py", "b.py"])
    route = routing.route_card(c)
    state = jq.routing_state(c, route)
    assert "A" in state
    assert "do it" in state
    assert "estimate_tokens: 40000" in state
    assert "auth" in state
    assert "write files: 2" in state
    assert route.legs[0].id in state
    assert "leg:" in state


def test_routing_state_is_clipped_to_the_cap():
    c = card(key="A", outcome="x" * 100000)
    route = routing.route_card(c)
    assert len(jq.routing_state(c, route)) <= jq.MAX_STATE_CHARS


# --- redaction ---------------------------------------------------------------------

def test_redact_removes_tokens_paths_urls_and_ips():
    secret = "sk-" + "a" * 40
    ip = ".".join(("10", "0", "0", "5"))
    text = ("token " + secret + " at /home/x/y and C:\\Users\\x\\y "
            "and https://host.example/x and " + ip)
    out = jq.redact(text, 1000)
    for leak in (secret, "/home/x/y", "C:\\Users\\x\\y", "host.example",
                 ip):
        assert leak not in out
    assert "[redacted]" in out


def test_redact_collapses_whitespace_and_clips():
    assert jq.redact("a  b\n\tc", 100) == "a b c"
    assert len(jq.redact("ab cd " * 50, 10)) == 10


def test_redact_never_raises_on_odd_input():
    assert jq.redact(None, 10) == ""
    assert jq.redact(12345, 100) == "12345"
    assert jq.redact(["x"], 100) == "['x']"
    assert isinstance(jq.redact(object(), 100), str)


