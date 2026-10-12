"""D-677: a dependent of an operator-waiting item shows its waitTip on hover.

The graph has no generic tooltip; page_graph_pan.js (PanDrag.install) copies the hovered node's
waitTip into the container's title (the browser's native tooltip) and clears it
on mouseout. Static check of the wiring + the waitTip text it feeds on.
Mutation: drop the mouseover line -> test fails.
"""
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "src" / "plangraph" / "web"


def test_wait_tip_is_wired_to_hover():
    src = (WEB / "page_graph_pan.js").read_text(encoding="utf-8")
    assert 'cy.on("mouseover", "node"' in src
    assert 'data("waitTip")' in src
    assert 'cy.on("mouseout", "node"' in src
    assert '"mouseleave"' in src  # leaving the canvas from a node clears it too


def test_wait_tip_text_is_built_for_dependents():
    src = (WEB / "page_graph.js").read_text(encoding="utf-8")
    assert '"waits on operator ("' in src and '") via "' in src
