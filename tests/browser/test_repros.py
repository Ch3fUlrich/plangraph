"""R1-R10 repros (design §3 of docs/plans/ui/2026-10-17-ui-plan.md).

One test per measured problem: it replays the row's "Repro" steps at the
viewport that row names and asserts the *fixed* behaviour with numbers taken
through ``tests/browser/probes.py``. Red against the current code carries
``xfail(strict=True, reason="plangraph:<OWNER>")`` so the fix item turns it
green; owner + observed value are in the lane REPORT. Expanded views open
through the URL hash (``#cpr=..&cph=..``, page_store.js) instead of clicking,
because clicking a project first moves the viewport (R4) and would mask the
behaviour under test.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

import pytest

from browser import probes
from browser.conftest import SIZES

pytestmark = pytest.mark.browser

ALL_PHASES = ["harbor|P1", "harbor|P2", "harbor|goal",
              "lighthouse|T1", "lighthouse|T2", "lighthouse|goal",
              "orchard|T1", "orchard|T2", "orchard|T3", "orchard|goal"]
HEADER_PX = 44                  # page_graph_pan.js HEADER_UNITS = clickable header strip
ELK_FAIL = "pg-test-elk-failure"    # the reason R10 makes the layout engine throw
INVALID_ENDPOINTS = re.compile(r"invalid endpoints", re.I)
DESKTOP, TABLET, PHONE = SIZES["desktop"], SIZES["tablet"], SIZES["phone"]


def hash_for(collapsed_projects, expanded_phases):
    """Hash that opens exactly those projects/phase cards (see page_store.js)."""
    return "#cpr=%s&cph=%s" % (
        ",".join(quote(p) for p in collapsed_projects),
        ",".join(quote(p) for p in ALL_PHASES if p not in expanded_phases))


# orchard|T1 = OR-1 + OR-2: no deps, no cross-project dep, no loop -> no R9 noise
OPEN_ORCHARD_T1 = hash_for(["harbor", "lighthouse"], ["orchard|T1"])


def header_point(page, node_id):
    """Screen point (canvas-relative px) inside *node_id*'s header strip."""
    box = probes.rendered_bbox(page, node_id)
    if not box:
        return None
    off = min(20.0, 0.8 * HEADER_PX * probes.viewport(page)["zoom"], max(1.0, box["h"] * 0.2))
    return {"x": box["x1"] + box["w"] / 2.0, "y": box["y1"] + off, "box": box}


def click_point(page, point):
    rect = probes.canvas_rect(page)
    page.mouse.click(rect["x"] + point["x"], rect["y"] + point["y"])


def click_header(page, node_id):
    """Click a node in its header strip (what a project/phase toggle needs)."""
    point = header_point(page, node_id)
    if point is not None:
        click_point(page, point)
    return point


def header_jump(page, stub_id, compound_id):
    """px the clicked header moves when *stub_id* expands into *compound_id* (V-GRAPH-3)."""
    stub = probes.rendered_bbox(page, stub_id)
    click_header(page, stub_id)
    page.wait_for_timeout(1200)
    probes.wait_layout_idle(page)
    after = probes.rendered_bbox(page, compound_id)
    jump = (round(after["x1"] - stub["x1"], 1), round(after["y1"] - stub["y1"], 1))
    return jump, "stub x1=%.1f y1=%.1f -> compound x1=%.1f y1=%.1f" % (
        stub["x1"], stub["y1"], after["x1"], after["y1"])


def graph_state(page):
    return page.evaluate(
        "() => ({collapsedProjects: Array.from(PlanUI.app.ui.get('collapsedProjects')).sort(),"
        " collapsedPhases: Array.from(PlanUI.app.ui.get('collapsedPhases')).sort(),"
        " sel: PlanUI.app.ui.get('sel')})")


def task_keys(page):
    return page.evaluate(
        "() => { const out = []; PlanUI.GraphView.getCy().nodes('.task-node')"
        ".forEach(n => out.push(n.data('key'))); return out; }")


def relayout_indicator_count(page):
    """How many 'the graph is relaying out' indicators the page exposes (V-GRAPH-6)."""
    return page.evaluate(
        "() => document.querySelectorAll('#graph-busy, .graph-busy, [data-pg-busy],"
        " .cy-busy, .busy-indicator, [aria-busy=true]').length")


@pytest.fixture
def console_page(browser, site, vendor_assets):
    """Open a page that keeps its own console log instead of failing the test for
    every message: R9/R10 measure what a broken graph *logs*, so the shared ``page``
    fixture - which turns any error/warning into a teardown failure - cannot be used.
    Nothing reaches past 127.0.0.1 here either."""
    contexts = []

    def open(elk_fails=False, size=DESKTOP, hash=""):
        context = browser.new_context(viewport={"width": size[0], "height": size[1]})
        contexts.append(context)
        pg = context.new_page()
        log = {"errors": [], "warnings": []}
        pg.on("console", lambda m: log["errors"].append(m.text) if m.type == "error"
              else (log["warnings"].append(m.text) if m.type in ("warning", "warn") else None))
        pg.on("pageerror", lambda e: log["errors"].append(str(e)))
        # ELK swapped for an engine that always rejects, so the graph really fails.
        stub = ("window.ELK = function () { this.layout = function () {"
                " return Promise.reject(new Error('%s')); }; };") % ELK_FAIL
        elk_url = next(u for u, path in vendor_assets.items() if "elk" in path.name.lower())

        def handle(route):
            url = route.request.url
            if elk_fails and url == elk_url:
                route.fulfill(status=200, content_type="application/javascript", body=stub)
            elif url.startswith(site):
                if url.endswith("/favicon.ico"):
                    route.fulfill(status=204, content_type="image/x-icon", body=b"")
                else:
                    route.continue_()
            elif url in vendor_assets:
                route.fulfill(status=200, content_type="application/javascript",
                              body=vendor_assets[url].read_bytes())
            else:
                route.abort()

        pg.route("**/*", lambda route: handle(route))
        pg.goto(site + hash)
        return pg, log

    yield open
    for context in contexts:
        context.close()


@pytest.mark.xfail(strict=True, reason="plangraph:UI-NARROW-LAYOUT")
@pytest.mark.parametrize("size", [TABLET], ids=["tablet"], indirect=["size"])
def test_r1_scrim_and_sidebar_at_1024(page, site):
    """R1 (UI-NARROW-LAYOUT): at 1024x768 no scrim, sidebar starts closed, and the
    first click on the graph reaches the graph (V-SIDE-1)."""
    page.goto(site)
    probes.wait_layout_idle(page)
    scrim = page.evaluate(
        "() => { const s = document.getElementById('sb-scrim'); if (!s) return null;"
        " const r = s.getBoundingClientRect(), cs = getComputedStyle(s);"
        " return {hidden: s.hidden, painted: cs.display !== 'none',"
        " area: Math.round(r.width * r.height)}; }")
    sidebar = page.evaluate(
        "() => ({width: Math.round(document.getElementById('sidebar')"
        ".getBoundingClientRect().width),"
        " sbopen: document.body.getAttribute('data-sbopen')})")
    rect = probes.canvas_rect(page)
    centre = page.evaluate(    # what actually receives a click on the canvas centre
        "(a) => { const el = document.elementFromPoint(a.x, a.y);"
        " return el ? (el.id || el.tagName.toLowerCase()) : null; }",
        {"x": rect["x"] + rect["width"] / 2, "y": rect["y"] + rect["height"] / 2})
    before = graph_state(page)
    click_point(page, header_point(page, "pc:lighthouse"))
    page.wait_for_timeout(1200)
    probes.wait_layout_idle(page)
    after = graph_state(page)

    assert scrim["painted"] is False or scrim["area"] == 0, (
        "scrim painted while closed: %s" % json.dumps(scrim))
    assert sidebar["sbopen"] == "0", "sidebar is open at load (%s)" % json.dumps(sidebar)
    assert centre == "cy", "element on top of the canvas centre is %r" % centre
    assert after["collapsedProjects"] != before["collapsedProjects"], (
        "first click did not reach the graph: collapsedProjects stayed %s"
        % json.dumps(before["collapsedProjects"]))


@pytest.mark.xfail(strict=True, reason="plangraph:UI-GRAPH-CLICK")
def test_r2_panel_keeps_node_visible(page, site):
    """R2 (UI-GRAPH-CLICK): clicking a task in the right 320 px docks the panel and
    pans so the node stays at least 24 px inside the visible canvas (V-GRAPH-2)."""
    page.goto(site + OPEN_ORCHARD_T1)
    probes.wait_layout_idle(page)
    page.wait_for_timeout(300)
    keys = task_keys(page)
    assert len(keys) == 2, "expected the 2 orchard T1 tasks, got %s" % json.dumps(keys)
    key = "t:" + keys[0]
    probes.pan_node_to(page, key, {"x": {"right": 150}})   # into the band the panel takes
    page.wait_for_timeout(400)
    rect_before = probes.canvas_rect(page)
    inside_before = probes.node_in_canvas(page, keys[0], -24)
    point = header_point(page, key)
    click_point(page, {"x": point["x"], "y": point["y"] + point["box"]["h"] / 2})
    page.wait_for_timeout(1200)
    probes.wait_layout_idle(page)
    rect_after = probes.canvas_rect(page)
    bbox = probes.rendered_bbox(page, key)

    assert rect_after["width"] < rect_before["width"], (
        "details panel did not dock: canvas stayed %s px" % rect_before["width"])
    assert inside_before, "the node was not inside before the click (test setup)"
    assert probes.node_in_canvas(page, keys[0], -24), (
        "node %s ends %.1f px outside the %.1f px canvas after docking (bbox %s)"
        % (keys[0], (bbox["x1"] + bbox["w"]) - rect_after["width"], rect_after["width"],
           json.dumps(bbox)))


@pytest.mark.xfail(strict=True, reason="plangraph:UI-GRAPH-CLICK")
def test_r3_first_click_no_viewport_reset(page, site):
    """R3 (UI-GRAPH-CLICK): the first task click opens the panel without resetting
    zoom or pan and without moving a node (V-GRAPH-1, V-GRAPH-4)."""
    page.goto(site + OPEN_ORCHARD_T1)
    probes.wait_layout_idle(page)
    page.wait_for_timeout(300)
    keys = task_keys(page)
    before, pos_before = probes.viewport(page), probes.model_positions(page)
    point = header_point(page, "t:" + keys[0])
    click_point(page, {"x": point["x"], "y": point["y"] + point["box"]["h"] / 2})
    page.wait_for_timeout(1200)
    probes.wait_layout_idle(page)
    after, pos_after = probes.viewport(page), probes.model_positions(page)
    moved = [i for i in pos_before if pos_after.get(i) and max(
        abs(a - b) for a, b in zip(pos_before[i], pos_after[i])) > 1]
    dzoom = abs(after["zoom"] - before["zoom"])
    dpan = max(abs(after["pan"]["x"] - before["pan"]["x"]),
               abs(after["pan"]["y"] - before["pan"]["y"]))

    assert graph_state(page)["sel"] == keys[0], "the click did not select %s" % keys[0]
    assert dzoom <= 0.001, "zoom changed %.6f -> %.6f (delta %.6f)" % (
        before["zoom"], after["zoom"], dzoom)
    assert dpan <= 1.0, "pan moved %.1f px" % dpan
    assert not moved, "nodes moved in model coordinates: %s" % json.dumps(moved)


@pytest.mark.xfail(strict=True, reason="plangraph:UI-GRAPH-EXPAND")
def test_r4_expand_keeps_header_position(page, site):
    """R4 (UI-GRAPH-EXPAND): expanding a project and then a phase keeps the clicked
    header at the same screen position within 4 px (V-GRAPH-3)."""
    page.goto(site)
    probes.wait_layout_idle(page)
    page.wait_for_timeout(300)
    for kind, stub_id, compound_id in [("project", "pc:orchard", "p:orchard"),
                                       ("phase", "phc:orchard|T1", "ph:orchard|T1")]:
        jump, where = header_jump(page, stub_id, compound_id)
        assert max(abs(d) for d in jump) <= 4.0, (
            "%s header jumped %s px (%s)" % (kind, json.dumps(jump), where))


@pytest.mark.xfail(strict=True, reason="plangraph:UI-GRAPH-EXPAND")
@pytest.mark.parametrize("gap_ms", [100, 300])
def test_r5_click_during_relayout(page, site, gap_ms):
    """R5 (UI-GRAPH-EXPAND): a click that lands while a relayout runs is applied to
    the element it was aimed at (by key), or dropped with the busy indicator still
    visible - never to a different element (V-GRAPH-6)."""
    page.goto(site)
    probes.wait_layout_idle(page)
    page.wait_for_timeout(300)
    aimed = probes.rendered_bbox(page, "pc:lighthouse")
    where = {"x": aimed["x1"] + aimed["w"] / 2, "y": aimed["y1"] + 20}
    click_header(page, "pc:harbor")             # starts a relayout that outlives the gap
    page.wait_for_timeout(gap_ms)
    hit = probes.hit_nodes(page, where["x"], where["y"])
    before = graph_state(page)
    click_point(page, where)
    page.wait_for_timeout(1500)
    probes.wait_layout_idle(page)
    after = graph_state(page)
    indicators = relayout_indicator_count(page)

    applied = "lighthouse" not in after["collapsedProjects"]
    dropped = after["collapsedProjects"] == before["collapsedProjects"] and indicators > 0
    assert applied or dropped, (
        "gap %d ms: click aimed at pc:lighthouse (%s px) was neither applied to it nor"
        " dropped behind an indicator: now hit=%s collapsedProjects %s -> %s, indicators=%d"
        % (gap_ms, json.dumps([round(where["x"], 1), round(where["y"], 1)]), json.dumps(hit),
           json.dumps(before["collapsedProjects"]), json.dumps(after["collapsedProjects"]),
           indicators))
    assert "orchard" in after["collapsedProjects"], "the click landed on another project"


@pytest.mark.parametrize("size", [PHONE], ids=["phone"], indirect=["size"])
def test_r6_phone_expanded_phase_has_tasks(page, site):
    """R6 (UI-GRAPH-EXPAND): at 390x844 an expanded phase draws its tasks at real
    size and no project box overlaps another (V-GRAPH-10)."""
    page.goto(site)
    probes.wait_layout_idle(page)
    page.wait_for_timeout(300)
    click_header(page, "pc:orchard")
    page.wait_for_timeout(1500)
    probes.wait_layout_idle(page)
    # the expanded card lands off-canvas; pan it to the middle like a user would
    probes.pan_node_to(page, "phc:orchard|T1", {"x": {"centre": True}, "y": {"centre": True}})
    page.wait_for_timeout(400)
    click_header(page, "phc:orchard|T1")
    page.wait_for_timeout(1500)
    probes.wait_layout_idle(page)

    boxes = probes.rendered_boxes(
        page, ".project-compound, .project-collapsed, .task-node")
    tasks = [b for b in boxes if b["id"].startswith("t:")]
    assert len(tasks) == 2, "expanded phase draws %d task nodes, expected 2" % len(tasks)
    degenerate = [{"id": t["id"], "w": round(t["w"], 1), "h": round(t["h"], 1)}
                  for t in tasks if not (t["w"] > 1 and t["h"] > 1)]
    assert not degenerate, "zero-size task nodes: %s" % json.dumps(degenerate)
    cards = [b for b in boxes if b["id"].split(":")[0] in ("p", "pc")]
    pairs = [(a["id"], b["id"]) for i, a in enumerate(cards) for b in cards[i + 1:]
             if a["x1"] < b["x2"] and b["x1"] < a["x2"] and a["y1"] < b["y2"] and b["y1"] < a["y2"]]
    assert not pairs, "project boxes overlap: %s" % json.dumps(pairs)


@pytest.mark.xfail(strict=True, reason="plangraph:UI-URL-HISTORY")
def test_r7_back_stays_in_app(page, site):
    """R7 (UI-URL-HISTORY): after expand + select, Back moves to the previous app
    step instead of leaving the app (V-ALL-5)."""
    page.goto(site + OPEN_ORCHARD_T1)
    probes.wait_layout_idle(page)
    page.wait_for_timeout(300)
    keys = task_keys(page)
    point = header_point(page, "t:" + keys[0])
    click_point(page, {"x": point["x"], "y": point["y"] + point["box"]["h"] / 2})
    page.wait_for_timeout(800)
    probes.wait_layout_idle(page)
    click_header(page, "pc:harbor")
    page.wait_for_timeout(1500)
    probes.wait_layout_idle(page)
    assert graph_state(page)["sel"] == keys[0], "selection lost before Back"
    url_before = page.url

    page.go_back()
    page.wait_for_timeout(800)

    assert page.url.startswith(site), "Back left the app: hash %r -> url %r" % (
        url_before.split("#", 1)[-1][:120], page.url)
    assert page.evaluate("() => !!document.getElementById('cy')"), "graph canvas gone"


@pytest.mark.xfail(strict=True, reason="plangraph:UI-FLAKE-FIXES")
def test_r8_knowledge_export_requested_only_when_present(page, site):
    """R8 (UI-FLAKE-FIXES): without a rendered export the page never asks for
    kg-export.json (so it can never 404) and the Knowledge tab stays visible with
    its empty-state reason (V-SHELL-3, V-KG-1)."""
    requested: list[str] = []
    page.on("request", lambda r: requested.append(r.url))
    page.goto(site)
    probes.wait_layout_idle(page)
    page.wait_for_timeout(800)
    kg_requests = [u for u in requested if u.endswith("kg-export.json")]
    tab = page.evaluate(
        "() => { const t = document.getElementById('tab-knowledge'); if (!t) return null;"
        " return {hidden: t.hidden, width: Math.round(t.getBoundingClientRect().width),"
        " text: t.textContent.trim()}; }")
    has_export = page.evaluate(
        "() => !!(window.PlanUI && PlanUI.Knowledge && PlanUI.Knowledge.get"
        " && PlanUI.Knowledge.get())")

    assert not has_export, "fixture snapshot has no knowledge export"
    assert not kg_requests, (
        "page asked for the export %d time(s) although the snapshot has none: %s"
        % (len(kg_requests), json.dumps(kg_requests)))
    assert tab["width"] > 0, "Knowledge tab is hidden (%s)" % json.dumps(tab)


@pytest.mark.xfail(strict=True, reason="plangraph:UI-FLAKE-FIXES")
def test_r9_expand_draws_no_invalid_endpoint_warnings(console_page):
    """R9 (UI-FLAKE-FIXES): expanding harbor and then harbor|P1 at 1440x900 logs no
    cytoscape 'invalid endpoints' warning (V-GRAPH-7)."""
    pg, log = console_page()
    probes.wait_layout_idle(pg)
    pg.wait_for_timeout(400)
    for node_id in ("pc:harbor", "phc:harbor|P1"):
        click_header(pg, node_id)
        pg.wait_for_timeout(1500)
        probes.wait_layout_idle(pg)
    bad = [w for w in log["warnings"] if INVALID_ENDPOINTS.search(w)]
    drawn = pg.evaluate(
        "() => { const cy = PlanUI.GraphView.getCy();"
        " return {edges: cy.edges().length, nodes: cy.nodes().length}; }")

    assert not bad, "%d 'invalid endpoints' warning(s) over %s: %s" % (
        len(bad), json.dumps(drawn), json.dumps(bad[:2]))


@pytest.mark.xfail(strict=True, reason="plangraph:UI-FLAKE-FIXES")
def test_r10_graph_failure_is_logged(console_page):
    """R10 (UI-FLAKE-FIXES): a failing layout logs the reason to the console and the
    canvas says so instead of failing silently (V-GRAPH-8)."""
    pg, log = console_page(elk_fails=True, hash=OPEN_ORCHARD_T1)
    pg.wait_for_timeout(2000)
    banner = pg.evaluate(
        "() => { const b = document.getElementById('graph-engine-banner');"
        " return b ? {hidden: b.hidden, text: b.textContent.trim()} : null; }")

    assert banner and banner["hidden"] is False, "no engine banner (%s)" % json.dumps(banner)
    errors = [e for e in log["errors"] if ELK_FAIL in e]
    assert errors, "layout failed with %r but the console logged %d error(s) naming it " \
                   "(all errors=%s)" % (ELK_FAIL, len(errors), json.dumps(log["errors"][:5]))
