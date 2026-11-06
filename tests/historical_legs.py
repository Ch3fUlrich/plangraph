"""Explicit historical route fixtures, never production admission policy."""
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn


@pytest.fixture
def historical_zen_legs(monkeypatch):
    """Exercise quota/transport behavior independently of D-4196 admission."""
    monkeypatch.setitem(plan_spawn.__kwdefaults__, 'legs', tuple(
        replace(l, disabled=None) if l.model.startswith('opencode/') else l
        for l in DEFAULT_LEGS))
