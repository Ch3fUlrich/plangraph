"""Explicit multi-leg fixtures for routing mechanics, not production writers."""
from dataclasses import replace

from plangraph.planner.legs import DEFAULT_LEGS


def fallthrough_legs():
    vehicles = {
        'opencode-deepseek-v4.1-flash-free', 'opencode-glm-5.3-flash-free',
        'openrouter-free', 'agy-gemini-4-argon', 'mimo-v2.6-flash',
        'mimo-v2.6-pro', 'deepseek',
    }
    return tuple(replace(leg, roles=(*leg.roles, 'implement'), disabled=None)
                 if leg.id in vehicles else leg for leg in DEFAULT_LEGS)


def install_fallthrough_legs(monkeypatch):
    """Give a mechanism test its own role-bearing legs, including subprocesses."""
    from plangraph.planner import legs, routing
    table = fallthrough_legs()
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', table)
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'legs', table)
    return table
