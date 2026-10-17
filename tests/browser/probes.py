"""JS-side probes against the live cytoscape instance (PlanUI.GraphView.getCy()).

Every helper runs a single ``page.evaluate``; task nodes are looked up as
``"t:" + key`` first, then the bare ``key`` (page_graph_view.js selectNode order).
"""
from __future__ import annotations

_GET_NODE = """
(key) => {
  const cy = PlanUI.GraphView.getCy();
  if (!cy) return null;
  let n = cy.getElementById("t:" + key);
  if (!n.length) n = cy.getElementById(key);
  return n.length ? n.id() : null;
}
"""


def viewport(page):
    """{'zoom': float, 'pan': {'x','y'}} of the cytoscape viewport."""
    return page.evaluate(
        "() => { const cy = PlanUI.GraphView.getCy(); if (!cy) return null;"
        " const p = cy.pan(); return {zoom: cy.zoom(), pan: {x: p.x, y: p.y}}; }"
    )


def _node_id(page, key):
    return page.evaluate(_GET_NODE, key)


def model_pos(page, key):
    """Model-space position {x, y} of the node for *key*, or None."""
    return page.evaluate(
        "(id) => { const cy = PlanUI.GraphView.getCy();"
        " if (!cy || !id) return null;"
        " const n = cy.getElementById(id); if (!n.length) return null;"
        " const p = n.position(); return {x: p.x, y: p.y}; }",
        _node_id(page, key),
    )


def rendered_bbox(page, key):
    """Rendered bounding box {x1, y1, w, h} (canvas origin) of *key*, or None."""
    return page.evaluate(
        "(id) => { const cy = PlanUI.GraphView.getCy();"
        " if (!cy || !id) return null;"
        " const n = cy.getElementById(id); if (!n.length) return null;"
        " const b = n.renderedBoundingBox(); return {x1: b.x1, y1: b.y1, w: b.w, h: b.h}; }",
        _node_id(page, key),
    )


def canvas_rect(page):
    """Client rect {x, y, width, height} of the graph canvas element (#cy)."""
    return page.evaluate(
        "() => { const el = document.getElementById('cy');"
        " if (!el) return null;"
        " const r = el.getBoundingClientRect();"
        " return {x: r.x, y: r.y, width: r.width, height: r.height}; }"
    )


def node_in_canvas(page, key, margin=0):
    """True when the rendered node box lies inside the canvas (plus *margin* px)."""
    return page.evaluate(
        "(a) => { const cy = PlanUI.GraphView.getCy(); if (!cy) return false;"
        " let n = cy.getElementById('t:' + a.key); if (!n.length) n = cy.getElementById(a.key);"
        " if (!n.length) return false;"
        " const b = n.renderedBoundingBox(); if (!(b.w > 0 && b.h > 0)) return false;"
        " return b.x1 >= -a.margin && b.y1 >= -a.margin"
        " && b.x1 + b.w <= cy.width() + a.margin && b.y1 + b.h <= cy.height() + a.margin; }",
        {"key": key, "margin": margin},
    )


def wait_layout_idle(page):
    """Wait for the ``window.__pgLayoutIdle`` hook, then await one idle round."""
    page.wait_for_function("() => typeof window.__pgLayoutIdle === 'function'")
    page.evaluate("() => window.__pgLayoutIdle()")
