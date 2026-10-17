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


def model_positions(page):
    """{node id: [x, y]} in model coordinates - what a relayout may legitimately move."""
    return page.evaluate(
        "() => { const pos = {}; PlanUI.GraphView.getCy().nodes().forEach(n => {"
        " const p = n.position(); pos[n.id()] = [p.x, p.y]; }); return pos; }")


def rendered_boxes(page, selector=""):
    """Rendered boxes {id, w, h, x1, y1, x2, y2} for *selector* (all nodes if empty)."""
    return page.evaluate(
        "(sel) => { const out = []; const cy = PlanUI.GraphView.getCy();"
        " (sel ? cy.nodes(sel) : cy.nodes()).forEach(n => { const b = n.renderedBoundingBox();"
        " out.push({id: n.id(), w: b.w, h: b.h, x1: b.x1, y1: b.y1,"
        " x2: b.x1 + b.w, y2: b.y1 + b.h}); }); return out; }",
        selector or "")


def hit_nodes(page, x, y):
    """Ids of nodes whose rendered box covers the canvas point (*x*, *y*) - the node a
    click at that point lands on *now*, which is what makes a dropped click measurable."""
    return page.evaluate(
        "(a) => { const out = []; PlanUI.GraphView.getCy().nodes().forEach(n => {"
        " const b = n.renderedBoundingBox();"
        " if (b.w > 0 && a.x >= b.x1 && a.x <= b.x1 + b.w && a.y >= b.y1 && a.y <= b.y1 + b.h)"
        " out.push(n.id()); }); return out; }",
        {"x": x, "y": y})


def pan_node_to(page, node_id, to):
    """Pan the canvas so *node_id*'s rendered centre lands on *to*: ``x``/``y`` each hold
    ``{'px': n}``, ``{'right': n}`` / ``{'bottom': n}`` (n px from that edge) or
    ``{'centre': True}``; a missing key keeps the current coordinate."""
    return page.evaluate(
        "(a) => { const cy = PlanUI.GraphView.getCy();"
        " const n = cy.getElementById(a.id); if (!n.length) return false;"
        " const b = n.renderedBoundingBox(), p = cy.pan();"
        " const cx = b.x1 + b.w / 2, cyPos = b.y1 + b.h / 2;"
        " const at = (v, cur, size) => !v ? cur"
        "   : v.centre ? size / 2 : v.right != null ? size - v.right"
        "   : v.bottom != null ? size - v.bottom : v.px != null ? v.px : cur;"
        " const tx = at(a.to.x, cx, cy.width()), ty = at(a.to.y, cyPos, cy.height());"
        " cy.pan({x: p.x + tx - cx, y: p.y + ty - cyPos}); return true; }",
        {"id": node_id, "to": to})
