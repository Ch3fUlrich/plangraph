"""Default OpenCode Go legs must name measured, served model IDs."""

from plangraph.planner.legs import DEFAULT_LEGS


# Measured with `opencode models` on 2026-11-06 (D-3952).
SERVED_OPENCODE_GO_IDS = frozenset({
    "opencode-go/deepseek-v4.1-flash",
    "opencode-go/gemini-4-argon",
    "opencode-go/glm-5.2",
    "opencode-go/qwen3.8-max",
    "opencode-go/muse-spark-1.3-contributor",
    "opencode-go/longcat-2.5-free",  # D-5648: model discovery 2026-11-13
})


def test_default_opencode_go_legs_name_served_ids():
    legs = [leg for leg in DEFAULT_LEGS if leg.id.startswith("opencode-go-")]
    assert legs
    for leg in legs:
        assert leg.model in SERVED_OPENCODE_GO_IDS, (leg.id, leg.model)
