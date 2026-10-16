"""Cross-family review legs: parity, picking, and gates."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_verdict as sv  # noqa: E402

from plangraph.review.family import (  # noqa: E402
    FAMILIES,
    LEGS,
    NEVER,
    FamilyError,
    FleetFull,
    NoEligibleLeg,
    check_cross_family,
    leg_for,
    model_family,
    pick_legs,
)

MODELS = [
    ("ovh/gpt-oss-120b", "openai"),
    ("gpt-oss-120b", "openai"),
    ("openai/gpt-5", "openai"),
    ("o3-mini", "openai"),
    ("o1-preview", "openai"),
    ("mimo-v2", "xiaomi"),
    ("vendor/mimo-7b", "xiaomi"),
    ("llama-4", "meta"),
    ("muse-spark", "meta"),
    ("meta/llama-3-70b", "meta"),
    ("deepseek-r1", "deepseek"),
    ("deepseek-chat", "deepseek"),
    ("qwen3-max", "alibaba"),
    ("Qwen3.8-Flash", "alibaba"),
    ("ovh/qwen3-8b", "alibaba"),
    ("gemini-3-pro", "google"),
    ("google/gemini-flash", "google"),
    ("claude-sonnet-5", "anthropic"),
    ("opus", "anthropic"),
    ("haiku-4", "anthropic"),
    ("mystery-1", None),
    ("gpt-claude-3", None),
    ("deepseek-llama-distill", None),
    ("claude-via-gpt-router", None),
]


def test_families_match_seat_verdict():
    assert FAMILIES == sv.FAMILIES


@pytest.mark.parametrize("model,want", MODELS)
def test_parity_with_seat_verdict(model, want):
    assert model_family(model) == want == sv.model_family(model)


@pytest.mark.parametrize("model,want", [(None, None), ("", None)])
def test_parity_edges(model, want):
    assert model_family(model) == want == sv.model_family(model)


def test_legs_and_never_shape():
    assert [leg.name for leg in LEGS] == ["openai", "qwen"]
    assert [leg.family for leg in LEGS] == ["openai", "alibaba"]
    assert LEGS[0].model == "ovh/gpt-oss-120b" and LEGS[0].kind == "gateway"
    assert LEGS[1].model == "Qwen3.8-Flash" and LEGS[1].kind == "qoder"
    assert NEVER == ("anthropic",)
    assert issubclass(NoEligibleLeg, ValueError)


def test_leg_for_served_and_requested():
    assert leg_for("ovh/gpt-oss-120b") == LEGS[0]
    assert leg_for("gpt-oss-120b") == LEGS[0]
    assert leg_for("Qwen3.8-Flash") == LEGS[1]
    assert leg_for("qwen3-max") == LEGS[1]
    assert leg_for("mystery-1") is None
    assert leg_for(None) is None
    assert leg_for("claude-opus-4") is None


def test_writer_openai_gets_qwen_only():
    assert pick_legs("openai") == [LEGS[1]]


def test_writer_alibaba_gets_openai_only():
    assert pick_legs("alibaba") == [LEGS[0]]


def test_writer_anthropic_prefers_openai_then_qwen():
    assert pick_legs("anthropic") == [LEGS[0]]
    assert pick_legs("anthropic", need=2) == [LEGS[0], LEGS[1]]


def test_writer_case_insensitive():
    assert pick_legs("OpenAI") == [LEGS[1]]
    assert pick_legs("ALIBABA") == [LEGS[0]]
    assert pick_legs("Anthropic", need=2) == [LEGS[0], LEGS[1]]
    assert check_cross_family("OpenAI", "Qwen3.8-Flash") == "alibaba"


def test_need_two_with_writer_openai_refused():
    with pytest.raises(NoEligibleLeg) as exc:
        pick_legs("openai", need=2)
    assert "openai" in str(exc.value)


def test_down_names_writer_and_set():
    with pytest.raises(NoEligibleLeg) as exc:
        pick_legs("openai", down=frozenset({"qwen"}))
    assert "openai" in str(exc.value) and "qwen" in str(exc.value)
    assert pick_legs("anthropic", down=frozenset({"qwen"})) == [LEGS[0]]
    assert pick_legs("anthropic", down=frozenset({"openai"})) == [LEGS[1]]
    with pytest.raises(NoEligibleLeg):
        pick_legs("anthropic", down=frozenset({"openai", "qwen"}))


def test_fleet_cap():
    with pytest.raises(FleetFull):
        pick_legs("anthropic", fleet_busy=4, fleet_cap=4)
    with pytest.raises(FleetFull):
        pick_legs("anthropic", need=2, fleet_busy=3, fleet_cap=4)
    assert pick_legs("anthropic", fleet_busy=3, fleet_cap=4) == [LEGS[0]]


def test_check_cross_family_ok():
    assert check_cross_family("openai", "Qwen3.8-Flash") == "alibaba"
    assert check_cross_family("alibaba", "ovh/gpt-oss-120b") == "openai"
    assert check_cross_family("anthropic", "gpt-oss-120b") == "openai"


def test_check_cross_family_refusals():
    with pytest.raises(FamilyError):
        check_cross_family("openai", "gpt-oss-120b")
    with pytest.raises(FamilyError):
        check_cross_family("meta", "llama-4")
    with pytest.raises(FamilyError):
        check_cross_family("openai", "claude-opus-4")
    with pytest.raises(FamilyError):
        check_cross_family("openai", "mystery-1")
    with pytest.raises(FamilyError):
        check_cross_family("openai", "gpt-claude-3")
    with pytest.raises(FamilyError):
        check_cross_family("OpenAI", "o3-mini")


def test_gateway_qwen_refused():
    with pytest.raises(FamilyError):
        check_cross_family("openai", "ovh/qwen3-8b")
    with pytest.raises(FamilyError):
        check_cross_family("anthropic", "OVH/Qwen-Max")
    assert leg_for("ovh/qwen3-8b") is None
