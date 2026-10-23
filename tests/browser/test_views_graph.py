"""VIEWS.md §3 Graph acceptance tags V-GRAPH-1..12 (lane UI-BROWSER-TESTS).

One test per tag; every assertion carries measured values (px, zoom, counts)
from ``probes``. Where the current page violates the spec that size is marked
``xfail(strict=True, reason="plangraph:<OWNER>")`` (owner in the reason,
measured value in the assertion message); when a fix lane makes it pass,
strict xfail reports XPASS as a failure, so that lane removes the marker.
A feature absent from the page altogether is ``skip``ped.

All tests load ``#so=0`` (drawer closed): below 1200 px the sidebar starts open
and a full-screen scrim eats the first graph click (repro R1, owner
plangraph:UI-NARROW-LAYOUT, tested in that lane) and must not block these.
"""
from __future__ import annotations

import pytest

from browser import probes
from browser.conftest import CDN_ELK_URL, SIZES

pytestmark = pytest.mark.browser

OWNER_CLICK = "UI-GRAPH-CLICK"
OWNER_EXPAND = "UI-GRAPH-EXPAND"
OWNER_URL = "UI-URL-HISTORY"
OWNER_FLAKE = "UI-FLAKE-FIXES"
ALL_SIZES = ("desktop", "tablet", "phone")


def _sizes(fail=None, only=None):
    """Parametrise the viewport; sizes listed in ``fail`` (size -> owner) get a
    strict xfail, so a size that starts passing turns into a visible XPASS."""
    fail = fail or {}
    params = []
    for name in (only or ALL_SIZES):
        marks = [pytest.mark.xfail(strict=True, reason=f"plangraph:{fail[name]}")] \
            if name in fail else []
        params.append(pytest.param(SIZES[name], id=name, marks=marks))
    return pytest.mark.parametrize("size", params, indirect=True)


def _settle(page, ms=350):
    """Let the 60 ms relayout debounce, the ELK run and any animation finish."""
    probes.wait_layout_idle(page)
    page.wait_for_timeout(ms)


def _boot(page, site, project=None, phase=None):
    """Cold load (drawer closed) and allow the R9 edge warning — owner
    plangraph:UI-FLAKE-FIXES — so this lane stays about the graph viewport.
    Then expand ``project`` and, if given, one phase inside it."""
    page.goto(site + "#so=0")
    page.pg_allow_console("warning", "invalid endpoints")
    _settle(page)
    if project:
        probes.click_node(page, f"pc:{project}")
        _settle(page)
    if phase:
        wanted = f"{project}|{phase}"
        if wanted in probes.ui_state(page)["collapsedPhases"]:   # toggle-safe
            probes.click_node(page, f"phc:{wanted}")
            _settle(page)
    return probes.ui_state(page)


# ---------------------------------------------------------------- 1 / 4 ----

@_sizes()
def test_v_graph_1_selecting_moves_no_node(page, site, size):
    """[V-GRAPH-1] Selecting a task moves no node in model coordinates and
    does not change zoom."""
    _boot(page, site, "lighthouse", "T1")
    key = probes.first_task_node(page)
    assert key, "no task node after expanding lighthouse/T1"
    probes.pan_node_frac(page, key, 0.4, 0.4)      # the user pans, then selects
    page.wait_for_timeout(150)
    before, vp0 = probes.task_model_pos(page), probes.viewport(page)
    centre = probes.node_center(page, key)
    page.mouse.click(centre["x"], centre["y"])
    _settle(page, 300)
    after, vp1 = probes.task_model_pos(page), probes.viewport(page)
    moved = probes.moved_nodes(before, after)
    assert not moved, (f"selection moved {len(moved)}/{len(before)} task nodes "
                       f"in model coords: {moved[:3]}")
    assert round(vp1["zoom"], 6) == round(vp0["zoom"], 6), \
        f"selection changed zoom {vp0['zoom']} -> {vp1['zoom']}"
    sel = probes.ui_state(page)["sel"]
    assert sel == key[2:], f"click on {key} selected {sel!r}"


@_sizes(fail={"desktop": OWNER_CLICK, "tablet": OWNER_CLICK})
def test_v_graph_4_details_panel_never_refits(page, site, size):
    """[V-GRAPH-4] Opening/closing the details panel and resizing the window
    never trigger a fit or a zoom reset."""
    page.goto(site + "#so=0")
    page.pg_allow_console("warning", "invalid endpoints")   # R9, UI-FLAKE-FIXES
    _settle(page)
    vp0 = probes.viewport(page)
    problems = []

    def drift(a, b):
        return round(abs(a["zoom"] - b["zoom"]), 4), round(max(
            abs(a["pan"]["x"] - b["pan"]["x"]), abs(a["pan"]["y"] - b["pan"]["y"])), 1)

    def report(what, a, b):
        dz, dp = drift(a, b)
        if dz > 0.0005 or dp > 1:
            problems.append(f"{what} moved the viewport: zoom {a['zoom']} -> "
                            f"{b['zoom']}, pan {a['pan']} -> {b['pan']}")

    # First task selection, before any pan/zoom by the user (repro R3): the
    # panel docks, the canvas shrinks and the viewport must stay as it was.
    page.evaluate("() => PlanPage.selectNode('lighthouse:LH-1')")
    _settle(page, 500)
    vp1 = probes.viewport(page)
    canvas1 = probes.visible_canvas(page)
    st = probes.ui_state(page)
    panel_open = page.evaluate(
        "() => { const d = document.getElementById('details'); return !!d && !d.hidden; }")
    assert st["sel"] == "lighthouse:LH-1" and panel_open, (
        f"the details panel did not actually open (sel={st['sel']!r}, open={panel_open});"
        " drift is only meaningful once the panel is shown")
    report(f"opening details (canvas {round(canvas1['w'])} px)", vp0, vp1)
    # Then a user pan, closing the panel and a window resize: same rule.
    probes.pan_node_frac(page, "pc:lighthouse", 0.35, 0.45)
    page.wait_for_timeout(200)
    vp2 = probes.viewport(page)
    assert drift(vp1, vp2)[1] > 1, (
        f"precondition: panning lighthouse to 35 %/45 % of the canvas did not "
        f"move the viewport ({vp1['pan']} -> {vp2['pan']})")
    page.keyboard.press("Escape")
    _settle(page, 400)
    page.set_viewport_size({"width": size[0] - 120, "height": size[1]})
    _settle(page, 400)
    report("closing details + resizing 120 px", vp2, probes.viewport(page))
    assert not problems, "; ".join(problems)


# --------------------------------------------------------------------- 2 ----

@_sizes(fail={"desktop": OWNER_CLICK, "phone": OWNER_CLICK})
def test_v_graph_2_covered_node_is_panned_into_view(page, site, size):
    """[V-GRAPH-2] If the details panel covers the selected node the view pans
    so the node is at least 24 px inside the visible canvas (at 1024 px the
    panel docks beside the canvas, so nothing is covered there)."""
    _boot(page, site, "lighthouse", "T1")
    key = "t:lighthouse:LH-1"
    wide = size[0] >= 1200          # docked panel: right edge; sheet: bottom
    probes.pan_node_frac(page, key, 0.97 if wide else 0.5,
                       0.5 if wide else 0.97)
    page.wait_for_timeout(150)
    centre = probes.node_center(page, key)
    page.mouse.click(centre["x"], centre["y"])
    _settle(page, 500)
    margin = probes.node_margin(page, key)
    pan = probes.viewport(page)["pan"]
    assert margin["min"] >= 24, (
        f"node ends {margin['min']} px inside the visible canvas (needs 24; "
        f"edges {margin}) and the view never panned (pan {pan})")


# --------------------------------------------------------------------- 3 ----

@_sizes(fail={n: OWNER_EXPAND for n in ALL_SIZES})
def test_v_graph_3_expand_keeps_header_position(page, site, size):
    """[V-GRAPH-3] Expanding/collapsing keeps the clicked header at the same
    screen position (within 4 px); if the box does not fit, zoom out to fit."""
    _boot(page, site)
    problems = []
    for card, comp, label in [("pc:harbor", "p:harbor", "project"),
                              ("phc:harbor|P1", "ph:harbor|P1", "phase")]:
        before = probes.node_center(page, card)
        page.mouse.click(before["x"], before["y"])
        _settle(page, 500)
        after = probes.node_center(page, comp)
        if not after:
            problems.append(f"expanding the {label} left {comp} out of the graph")
            break
        dx = round((after["canvasX"] + after["x1"]) - (before["canvasX"] + before["x1"]))
        dy = round((after["canvasY"] + after["y1"]) - (before["canvasY"] + before["y1"]))
        if abs(dx) > 4 or abs(dy) > 4:
            problems.append(
                f"expanding the {label} moved its header top-left {dx} px x / "
                f"{dy} px y (limit 4 px), box now {round(after['w'])}x{round(after['h'])} px")
        fit = probes.node_margin(page, comp)
        zoom = probes.viewport(page)["zoom"]
        if fit["min"] < 0 and zoom > 0.3:
            problems.append(
                f"the expanded {label} box ({round(after['w'])} px wide) sticks "
                f"{abs(fit['min'])} px outside the canvas but the view never "
                f"zoomed out to fit (zoom still {zoom})")
    assert not problems, "; ".join(problems)


# --------------------------------------------------------------------- 5 ----

@_sizes(fail={n: OWNER_EXPAND for n in ALL_SIZES})
def test_v_graph_5_focus_centres_target(page, site, size):
    """[V-GRAPH-5] A focus action expands what is needed, then centres the
    target at zoom >= 0.75."""
    _boot(page, site, "lighthouse", "T1")
    page.evaluate("() => PlanPage.selectNode('lighthouse:LH-3')")
    _settle(page, 400)
    # A dependency link inside the project just opened: the spec lists "a
    # details link" as a focus source.
    links = page.locator('#details .keylink[title^="lighthouse:"]')
    assert links.count() > 0, "the details panel shows no dependency link to focus from"
    links.first.click()
    _settle(page, 700)
    st = probes.ui_state(page)
    zoom = probes.viewport(page)["zoom"]
    centre = probes.node_center(page, st["sel"])
    assert centre, f"focus left {st['sel']!r} with no node in the graph"
    off_x = round(abs(centre["x"] - (centre["canvasX"] + centre["canvasW"] / 2)))
    off_y = round(abs(centre["y"] - (centre["canvasY"] + centre["canvasH"] / 2)))
    tol = round(min(centre["canvasW"], centre["canvasH"]) * 0.12)
    assert zoom >= 0.75, f"focus zoomed out to {zoom} (needs >= 0.75)"
    assert st["focusProject"] in (None, "lighthouse"), \
        f"focus of a lighthouse task left focusProject={st['focusProject']!r}"
    assert off_x <= tol and off_y <= tol, (
        f"focused {st['sel']} is {off_x} px x / {off_y} px y from the canvas "
        f"centre at zoom {zoom} (tolerance {tol} px, node box "
        f"{round(centre['w'])}x{round(centre['h'])})")


# --------------------------------------------------------------------- 6 ----

@pytest.mark.xfail(strict=True, reason=f"plangraph:{OWNER_EXPAND}: the page has no busy "
                         "indicator at all (no [aria-busy]/busy/spinner element "
                         "in src/plangraph/web, measured busyEls=[] at 100 ms "
                         "into a relayout at all three sizes)")
@_sizes()
def test_v_graph_6_click_during_layout_lands_by_key(page, site, size):
    """[V-GRAPH-6] While a relayout runs the graph shows a busy indicator and a
    click applies to the element clicked (by key), never a different one."""
    _boot(page, site)
    harbor = probes.node_center(page, "pc:harbor")
    page.mouse.click(harbor["x"], harbor["y"])
    page.wait_for_timeout(100)                       # mid-relayout
    busy = page.locator("[aria-busy], [class*=busy], [class*=spinner]").count()
    assert busy >= 1, "no busy indicator is shown while the layout runs"
    orchard = probes.node_center(page, "pc:orchard")
    page.mouse.click(orchard["x"], orchard["y"])
    _settle(page, 600)
    collapsed = probes.ui_state(page)["collapsedProjects"]
    assert "orchard" not in collapsed and "harbor" not in collapsed, (
        f"two header clicks expanded {sorted(collapsed)} still collapsed — "
        f"the second click did not land on the clicked element by key")


# --------------------------------------------------------------------- 7 ----

@_sizes(fail={n: OWNER_FLAKE for n in ALL_SIZES})
def test_v_graph_7_selection_dims_the_rest(page, site, size):
    """[V-GRAPH-7] The selected node is highlighted, its direct deps and
    dependents emphasised, the rest dimmed."""
    _boot(page, site, "harbor", "P1")
    key = "t:harbor:HB-30"
    probes.pan_node_frac(page, key, 0.5, 0.5)
    page.wait_for_timeout(150)
    centre = probes.node_center(page, key)
    page.mouse.click(centre["x"], centre["y"])
    _settle(page, 500)
    d = probes.selection_emphasis(page, key)
    assert d["selected"] == 1, f"{d['selected']} nodes carry :selected"
    assert d["dimmed"] >= 1, (
        f"selecting {key} dimmed {d['dimmed']} of {d['tasks']} task nodes and "
        f"{d['fullUnrelated']} unrelated ones stay at opacity 1 "
        f"(edges {d['edgeOpacity']}, target {d['targetOpacity']})")


# --------------------------------------------------------------------- 8 ----

@_sizes(fail={"phone": OWNER_URL})
def test_v_graph_8_selection_survives_reload(page, site, size):
    """[V-GRAPH-8] Selection survives collapse/re-expand, page switches and
    reload; after a reload the task's phase is expanded and it is in view."""
    _boot(page, site, "lighthouse", "T1")
    key = "t:lighthouse:LH-1"
    probes.pan_node_frac(page, key, 0.4, 0.4)
    centre = probes.node_center(page, key)
    page.mouse.click(centre["x"], centre["y"])
    _settle(page, 400)
    assert probes.ui_state(page)["sel"] == "lighthouse:LH-1", "selection lost"
    assert "lighthouse%3ALH-1" in page.evaluate("() => location.hash"), \
        "the selection is not in the URL"
    page.reload(wait_until="domcontentloaded")
    probes.wait_layout_idle(page)
    st = probes.ui_state(page)
    zoom = probes.viewport(page)["zoom"]
    centre = probes.node_center(page, st["sel"] or "")
    assert st["sel"] == "lighthouse:LH-1", f"reload lost sel={st['sel']!r}"
    assert "lighthouse|T1" not in st["collapsedPhases"], (
        f"after reload the selected task's phase is still collapsed "
        f"({st['collapsedPhases']})")
    assert centre, (f"after reload the selected task has no node in the graph "
                    f"(focusProject={st['focusProject']!r}, zoom {zoom})")
    assert probes.node_margin(page, st["sel"])["min"] >= 0, (
        f"selected task is outside the canvas after reload at zoom {zoom}")


# --------------------------------------------------------------------- 9 ----

@_sizes(fail={n: OWNER_FLAKE for n in ALL_SIZES})
def test_v_graph_9_double_click_equals_single(page, site, size):
    """[V-GRAPH-9] A double click does exactly what a single click does."""
    _boot(page, site, "lighthouse", "T1")
    key = "t:lighthouse:LH-1"
    probes.pan_node_frac(page, key, 0.4, 0.4)
    page.wait_for_timeout(150)
    single = probes.node_center(page, key)
    page.mouse.click(single["x"], single["y"])
    _settle(page, 400)
    after_single = probes.ui_state(page)
    page.evaluate("() => PlanUI.app.ui.set('sel', null)")
    _settle(page, 300)
    probes.pan_node_frac(page, key, 0.4, 0.4)
    page.wait_for_timeout(150)
    dbl = probes.node_center(page, key)
    page.mouse.dblclick(dbl["x"], dbl["y"])
    _settle(page, 600)
    after_double = probes.ui_state(page)
    keys = ("sel", "expanded", "collapsedPhases", "collapsedProjects")
    diff = {k: (after_single[k], after_double[k]) for k in keys
            if sorted(after_single[k] or []) != sorted(after_double[k] or [])}
    assert not diff, (
        f"a double click does more than a single one: {diff} "
        f"(as (single, double) tuples on lighthouse/LH-1)")


# -------------------------------------------------------------------- 10 ----

@_sizes(only=("phone",))
def test_v_graph_10_phone_expanded_phase_boxes(page, site, size):
    """[V-GRAPH-10] At 390x844 an expanded phase shows its tasks (no zero-size
    nodes) and no box overlaps another box."""
    _boot(page, site, "harbor", "P1")
    d = probes.box_layout(page)
    assert d["nTasks"] > 0, "expanding harbor/P1 produced no task nodes"
    assert not d["zero"], f"{len(d['zero'])} zero-size task nodes: {d['zero'][:3]}"
    assert d["overlaps"] == 0, (
        f"{d['overlaps']} box pairs overlap (canvas {d['canvas']}, "
        f"{d['nTasks']} tasks at {d['sample']}): {d['pairs']}")


# -------------------------------------------------------------------- 11 ----

@_sizes(fail={n: OWNER_FLAKE for n in ALL_SIZES})
def test_v_graph_11_layout_failure_is_reported(page, site, size):
    """[V-GRAPH-11] If the graph library or the layout fails, the canvas shows
    "graph unavailable: <reason>" and the console has the error."""
    seen = probes.watch_console(page)
    # Deliberately broken dependency: the layout library request is aborted, so
    # the expected resource error is allowed for this test only (R10 owner).
    page.pg_allow_console("error", "net::ERR_FAILED")
    page.pg_allow_console("error", "Failed to load resource")
    page.route(CDN_ELK_URL, lambda route: route.abort())
    page.goto(site + "#so=0")
    page.wait_for_timeout(2500)
    text = probes.engine_message(page)
    errors = [t for kind, t in seen if kind == "error"
              and "Failed to load resource" not in t]
    assert "graph unavailable" in text["text"], (
        f"the layout library is blocked but nothing says 'graph unavailable: "
        f"<reason>' — canvas text {text['canvas']!r}, banner {text['banner']!r}")
    assert errors, (
        f"the swallowed layout failure logged no error line (console errors: "
        f"{len(errors)}; canvas {text['canvas']!r})")


# -------------------------------------------------------------------- 12 ----

@_sizes(fail={n: OWNER_FLAKE for n in ALL_SIZES})
def test_v_graph_12_no_invalid_endpoint_edges(page, site, size):
    """[V-GRAPH-12] No edge with an invalid endpoint is created (no library
    warning)."""
    seen = probes.watch_console(page)
    _boot(page, site, "harbor", "P1")
    _settle(page, 800)
    bad = [t for _k, t in seen if "invalid endpoints" in t]
    ids = [t.split("`")[1] for t in bad if "`" in t]
    assert not bad, (
        f"{len(bad)} edges were created with invalid endpoints after expanding "
        f"harbor/P1 ({len(probes.task_model_pos(page))} task nodes): {ids[:4]}")
