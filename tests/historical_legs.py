"""Explicit historical route fixtures, never production admission policy."""
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn


def without_d4196(legs=DEFAULT_LEGS):
    """Lift only the D-4196 disable; any other reason keeps its leg disabled."""
    return tuple(replace(l, disabled=None) if 'D-4196' in (l.disabled or '') else l for l in legs)


@pytest.fixture
def historical_zen_legs(monkeypatch):
    """Exercise quota/transport behavior independently of D-4196 admission."""
    monkeypatch.setitem(plan_spawn.__kwdefaults__, 'legs', without_d4196())
    from plangraph.spawner import target
    monkeypatch.setattr(target, 'DEFAULT_LEGS', without_d4196())
