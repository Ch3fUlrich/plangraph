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

def ui_state(page):
    """Selected key, collapse sets, expanded set and focus from the live store."""
    return page.evaluate(
        "() => { const u = PlanUI.app && PlanUI.app.ui; if (!u) return null;"
        " return {sel: u.get('sel'), view: u.get('view'), focusProject: u.get('focusProject'),"
        " collapsedProjects: Array.from(u.get('collapsedProjects')),"
        " collapsedPhases: Array.from(u.get('collapsedPhases')),"
        " expanded: Array.from(u.get('expanded')), hash: location.hash}; }"
    )


def first_task_node(page):
    """Id of the first task node in the current graph, or None."""
    return page.evaluate("() => { const n = PlanUI.GraphView.getCy()"
                         "                        .nodes('.task-node')[0];"
                         "                        return n ? n.id() : null; }")


def task_model_pos(page):
    """{nodeId: {x, y}} for every task node — model coordinates."""
    return page.evaluate(
        "() => { const cy = PlanUI.GraphView.getCy(); const out = {};"
        " if (!cy) return out; cy.nodes('.task-node').forEach(n => {"
        " const p = n.position(); out[n.id()] = {x: p.x, y: p.y}; }); return out; }"
    )


def moved_nodes(before, after, tol=0.5):
    """Ids whose model position differs by more than *tol*."""
    return sorted(k for k in before
                  if k in after and max(abs(before[k]["x"] - after[k]["x"]),
                                        abs(before[k]["y"] - after[k]["y"])) > tol)


def node_center(page, key):
    """Page-viewport centre of the rendered node, or None when absent."""
    return page.evaluate(
        "(a) => { const cy = PlanUI.GraphView.getCy(); if (!cy) return null;"
        " let n = cy.getElementById('t:' + a); if (!n.length) n = cy.getElementById(a);"
        " if (!n.length) return null;"
        " const b = n.renderedBoundingBox(), r = cy.container().getBoundingClientRect();"
        " return {x: r.left + b.x1 + b.w / 2, y: r.top + b.y1 + b.h / 2,"
        " x1: b.x1, y1: b.y1, w: b.w, h: b.h,"
        " canvasX: r.left, canvasY: r.top, canvasW: r.width, canvasH: r.height}; }",
        key,
    )


def click_node(page, key):
    """Mouse-click the centre of *key*; returns the pre-click node_center dict."""
    c = node_center(page, key)
    assert c, f"node {key!r} is not in the graph"
    page.mouse.click(c["x"], c["y"])
    return c


def pan_node_frac(page, key, fx, fy):
    """Pan (no zoom) until *key*'s centre sits at canvas fraction (fx, fy)."""
    page.evaluate(
        "(a) => { const cy = PlanUI.GraphView.getCy();"
        " const n = cy.getElementById(a.key); if (!n.length) return false;"
        " const r = cy.container().getBoundingClientRect(), p = n.renderedPosition();"
        " cy.pan({x: cy.pan().x + (r.width * a.fx - p.x), y: cy.pan().y + (r.height * a.fy - p.y)});"
        " return true; }",
        {"key": key, "fx": fx, "fy": fy},
    )


def visible_canvas(page):
    """Canvas rect plus the details panel's covered flag (docked or sheet)."""
    return page.evaluate(
        "() => { const c = document.getElementById('cy');"
        " const d = document.getElementById('details');"
        " if (!c) return null;"
        " const a = c.getBoundingClientRect();"
        " const out = {x: a.x, y: a.y, w: a.width, h: a.height, covered: false};"
        " if (d && !d.hidden) { const b = d.getBoundingClientRect();"
        " const ox = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));"
        " const oy = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));"
        " out.covered = ox > 0 && oy > 0; out.panel = {x: b.x, y: b.y, w: b.width, h: b.height}; }"
        " return out; }"
    )


def node_margin(page, key):
    """Smallest px distance from the node box to the edge of the unobstructed
    canvas (negative when it sticks out / is covered by the panel)."""
    return page.evaluate(
        "(a) => { const cy = PlanUI.GraphView.getCy();"
        " let n = cy.getElementById('t:' + a.key); if (!n.length) n = cy.getElementById(a.key);"
        " if (!n.length) return null;"
        " const b = n.renderedBoundingBox(), r = cy.container().getBoundingClientRect();"
        " let right = r.width - (b.x1 + b.w), left = b.x1, bottom = r.height - (b.y1 + b.h), top = b.y1;"
        " const d = document.getElementById('details');"
        " if (d && !d.hidden) { const q = d.getBoundingClientRect();"
        "   if (q.left > r.left && q.left < r.right) right = Math.min(right, q.left - r.left - (b.x1 + b.w));"
        "   if (q.top > r.top && q.top < r.bottom) bottom = Math.min(bottom, q.top - r.top - (b.y1 + b.h)); }"
        " return {left: Math.round(left), right: Math.round(right),"
        " top: Math.round(top), bottom: Math.round(bottom),"
        " min: Math.round(Math.min(left, right, top, bottom))}; }",
        {"key": key},
    )


def watch_console(page):
    """Attach an extra console listener; returns the growing [(type, text)] list."""
    seen: list[tuple[str, str]] = []
    page.on("console", lambda m: seen.append((m.type, m.text)))
    return seen


def count_matching(seen, needle):
    """How many captured console lines contain *needle*."""
    return sum(1 for _kind, text in seen if needle in text)


def box_layout(page, selector=".task-node, .phase-compound, .project-compound"):
    """Rendered boxes of *selector*: task count, zero-size tasks, and overlaps
    between boxes that are not ancestor/descendant of one another."""
    return page.evaluate(
        """(sel) => { const cy = PlanUI.GraphView.getCy(); const list = [];
        cy.nodes(sel).forEach(n => { const b = n.renderedBoundingBox();
          list.push({id: n.id(), anc: n.ancestors().map(a => a.id()),
                     w: Math.round(b.w), h: Math.round(b.h),
                     x1: b.x1, y1: b.y1, x2: b.x1 + b.w, y2: b.y1 + b.h}); });
        const tasks = list.filter(x => x.id.startsWith('t:'));
        const zero = tasks.filter(x => x.w < 1 || x.h < 1).map(x => [x.id, x.w, x.h]);
        const pairs = [];
        for (let i = 0; i < list.length; i++) for (let j = i + 1; j < list.length; j++) {
          const a = list[i], b = list[j];
          if (a.anc.includes(b.id) || b.anc.includes(a.id)) continue;
          const w = Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1);
          const h = Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1);
          if (w > 0 && h > 0) pairs.push([a.id, b.id, Math.round(w), Math.round(h)]); }
        return {nTasks: tasks.length, zero, overlaps: pairs.length,
                pairs: pairs.slice(0, 5), sample: tasks.slice(0, 3).map(x => [x.w, x.h]),
                canvas: [Math.round(cy.width()), Math.round(cy.height())]}; }""",
        selector)


def selection_emphasis(page, node_id):
    """Opacity census after selecting *node_id*: dimmed (<= 0.3) task nodes,
    unrelated ones still at full opacity, plus neighbours and edge opacity."""
    return page.evaluate(
        """(k) => { const cy = PlanUI.GraphView.getCy(); const n = cy.getElementById(k);
        const op = x => Math.round(parseFloat(x.style('opacity')) * 100) / 100;
        const nb = {}; n.neighborhood('node').forEach(x => { nb[x.id()] = 1; });
        let tasks = 0, dimmed = 0, fullUnrelated = 0, fullNeighbours = 0;
        cy.nodes('.task-node').forEach(x => { tasks++; const o = op(x);
          if (o <= 0.3) dimmed++;
          else if (o >= 1 && x.id() !== k && !nb[x.id()]) fullUnrelated++;
          if (nb[x.id()] && o >= 1) fullNeighbours++; });
        return {selected: cy.nodes(':selected').length, targetOpacity: op(n),
                tasks, dimmed, fullUnrelated, neighbours: Object.keys(nb).length,
                fullNeighbours, edgeOpacity: op(cy.edges().first())}; }""",
        node_id)


def engine_message(page):
    """Text shown when the graph engine/layout failed (canvas + banner)."""
    return page.evaluate(
        """() => { const cy = document.getElementById('cy');
        const b = document.querySelector('[class*=banner], [id*=banner], [class*=engine]');
        const banner = b ? b.textContent.trim() : '';
        const canvas = cy ? (cy.innerText || '').trim() : '';
        return {canvas, banner, text: (canvas + ' ' + banner).toLowerCase()}; }""")
