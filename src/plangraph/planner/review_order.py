"""D-3901 review groups; measured order breaks ties within a group."""


def review_key(leg, tier_idx, pos):
    if leg.tier == "free":
        return (0, leg.id != "qodercli-qwen3.8-flash", tier_idx, pos)
    priority = (0 if leg.id == "qodercli-qwen3.8-flash" else
                1 if leg.family == "mimo" else
                2 if leg.family == "deepseek" else
                4 if "haiku" in leg.model.lower() else 3)
    return (1, priority, tier_idx, pos)
