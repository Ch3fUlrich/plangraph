"""W1 smoke: the served page boots, the graph lays out, nothing errors.

One test per viewport size; console/pageerror/external-request discipline is
enforced by the ``page`` fixture teardown (tests/browser/conftest.py).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from browser import probes
from browser.conftest import for_sizes

pytestmark = pytest.mark.browser


@for_sizes
def test_smoke_graph_boots(page, site):
    page.goto(site)
    probes.wait_layout_idle(page)
    node_count = page.evaluate(
        "() => { const cy = PlanUI.GraphView.getCy(); return cy ? cy.nodes().length : 0; }"
    )
    assert node_count > 0, "graph canvas has no nodes after layout idle"


SHOT_DIR = Path(os.environ.get("PG_UI_SHOTS", "out/ui-shots"))


@for_sizes
def test_smoke_screenshot(page, site, size):
    """D-323: a full-viewport screenshot per size, uploaded as a CI artifact."""
    page.goto(site)
    probes.wait_layout_idle(page)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = SHOT_DIR / f"graph-{size[0]}x{size[1]}.png"
    page.screenshot(path=str(out))
    assert out.stat().st_size > 0
