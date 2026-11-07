"""D-4812/D-297: a measured Claude leg must not outrank unmeasured non-Claude legs."""
from plangraph.planner import routing
from plangraph.planner.legs import DEFAULT_LEGS


def test_orchestrate_claude_leg_last_even_when_only_it_is_measured():
    legs = tuple(l for l in DEFAULT_LEGS if "orchestrate" in l.roles and not l.disabled)
    claude = [l for l in legs if l.claude_family]
    assert claude and len(legs) > len(claude)
    rows = {c.model: {"model_id": c.model, "aa_intelligence": 99,
                      "cost_per_task_usd": 0, "verified_tok_per_s": 99} for c in claude}
    plan = routing.plan_spawn({"estimate_tokens": 1000}, role="orchestrate", legs=legs,
                              benchmark=rows,
                              leg_reader=lambda *a, **k: {"state": "available", "evidence": None})
    flags = [l.claude_family for l in plan.legs]
    assert flags == sorted(flags), [l.id for l in plan.legs]
