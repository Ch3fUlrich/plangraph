"""UI-BROWSER-TESTS (W2c): one test per V-tag for docs/ui/VIEWS.md sections 5-12
(table, needs-you, decisions, activity, progress, knowledge, fleet, settings).

Each test asserts the SPEC against the current page (src/plangraph/web): a tag
whose behaviour is wrong today is ``xfail(strict=True)`` with the owning item;
when a fix lane makes it pass, strict xfail reports XPASS as a failure, so
that lane removes the marker. A feature absent from the page entirely is
``skip``. Sizes default to all three SIZES unless the tag names one.
Assertions carry measured probe values.
"""
from __future__ import annotations

import json

import pytest

from browser import probes
from browser.conftest import SIZES, for_sizes

pytestmark = pytest.mark.browser


def _show(page, name):
    page.evaluate("(n) => PlanUI.Pages.show(n)", name)
    page.wait_for_timeout(120)


def _load(page, site):
    page.goto(site)
    probes.wait_layout_idle(page)


# ---------------------------------------------------------------- Section 5: Table
@for_sizes
@pytest.mark.xfail(strict=True, reason="plangraph:UI-FLAKE-FIXES")
def test_v_tab_1(page, site):
    """V-TAB-1: the global search filters the table while it is shown.

    Typing into #search updates ui.query but the table is never re-rendered on a
    query change, so the visible rows do not follow the live search.
    """
    _load(page, site)
    _show(page, "table")
    initial = page.evaluate(
        "() => ({count: (document.querySelector('#list-content .tt-count')||{}).textContent,"
        " rows: document.querySelectorAll('#list-content tbody tr').length})")
    page.evaluate("() => { const s=document.getElementById('search'); s.value='zzz-no-such-task';"
                  " s.dispatchEvent(new Event('input')); }")
    page.wait_for_timeout(220)  # > the 150 ms search debounce
    after = page.evaluate(
        "() => ({count: (document.querySelector('#list-content .tt-count')||{}).textContent,"
        " rows: document.querySelectorAll('#list-content tbody tr').length})")
    assert after["rows"] < initial["rows"], (
        f"live search did not filter the table: rows stayed {initial['rows']} -> {after['rows']} "
        f"(count '{initial['count']}' vs '{after['count']}')"
    )


@for_sizes
@pytest.mark.xfail(strict=True, reason="plangraph:UI-URL-HISTORY")
def test_v_tab_2(page, site):
    """V-TAB-2: filters, sort, grouping and collapsed groups are in the URL.

    Grouping is a table-local view state never written to the URL, so it does
    not survive a reload.
    """
    _load(page, site)
    _show(page, "table")
    page.evaluate(
        "() => { const s=document.querySelector('#list-content .tt-sel'); s.value='phase';"
        " s.dispatchEvent(new Event('change')); }"
    )
    page.wait_for_timeout(120)
    grp_before = page.evaluate("() => PlanUI.TablePage.view.group")
    url = page.evaluate("() => location.search")
    page.reload()
    probes.wait_layout_idle(page)
    _show(page, "table")
    grp_after = page.evaluate("() => PlanUI.TablePage.view.group")
    assert grp_after == grp_before == "phase", (
        f"grouping not restored by URL: before='{grp_before}', after reload='{grp_after}', search='{url}'")


@for_sizes
def test_v_tab_3(page, site):
    """V-TAB-3: selecting a row selects the item (shared with graph + details)."""
    _load(page, site)
    _show(page, "table")
    got = page.evaluate(
        "() => { const tr=document.querySelector('#list-content tbody tr');"
        " if (!tr) return null; const b=tr.querySelector('.tc-open');"
        " b.click(); return {key: tr.getAttribute('data-key'),"
        " selected: PlanPage.state.selected, uiSel: PlanUI.app.ui.get('sel')} }")
    assert got is not None, "table rendered no rows"
    details_text = page.evaluate(
        "() => { const d=document.getElementById('details'); return d ? (d.textContent||'').slice(0,120) : ''; }"
    )
    tail = got["key"].split(":")[-1]
    assert got["selected"] == got["key"], f"row click selected {got['selected']!r}, expected {got['key']!r}"
    assert got["uiSel"] == got["key"], f"store sel {got['uiSel']!r} not synced to {got['key']!r}"
    assert tail in details_text, f"details panel does not mention {tail!r}: {details_text!r}"


def test_v_tab_4(page, site):
    """V-TAB-4 (390x844): rows become cards and stay usable."""
    _load(page, site)
    page.set_viewport_size({"width": SIZES["phone"][0], "height": SIZES["phone"][1]})
    _show(page, "table")
    info = page.evaluate(
        "() => { const tr=document.querySelector('#list-content tbody tr');"
        " if (!tr) return null; const td=tr.querySelector('td');"
        " b = tr.querySelector('.tc-open'); b.click();"
        " return {tdDisplay: getComputedStyle(td).display, trDisplay: getComputedStyle(tr).display,"
        " hasLabel: td.getAttribute('data-label'), key: tr.getAttribute('data-key'),"
        " selected: PlanPage.state.selected, scrollW: document.documentElement.scrollWidth,"
        " innerW: window.innerWidth} }")
    assert info is not None, "no table rows at 390x844"
    assert info["trDisplay"] != "table-row", f"row not a card at phone: display={info['trDisplay']}"
    assert info["tdDisplay"] not in ("table-cell",), f"cell not a card at phone: display={info['tdDisplay']}"
    assert info["hasLabel"], "card cell lost its data-label (no column name)"
    assert info["selected"] == info["key"], f"card tap did not select: {info['selected']!r} vs {info['key']!r}"
    assert info["scrollW"] <= info["innerW"], f"horizontal overflow: {info['scrollW']} > {info['innerW']}"


# ---------------------------------------------------------- Section 6: Needs you
@for_sizes
@pytest.mark.xfail(strict=True, reason="plangraph:UI-FLAKE-FIXES")
def test_v_need_1(page, site):
    """V-NEED-1: the unblocks count equals the keeper's dependency graph.

    The needs-you table shows no per-row unblocks count at all (columns are
    Type/ID/Title/Project/Prio/State/Age/Open).
    """
    _load(page, site)
    _show(page, "needs")
    expect = page.evaluate(
        "() => { const vm=PlanUI.app.vm; let best=null;"
        " (vm.needsYou||[]).forEach(function(e){ if (e.text!==undefined) return;"
        " const n=(e.unblocks||[]).length; if (n>0 && (!best||n>best.n)) best={id:e.key, n:n}; });"
        " return best; }"
    )
    assert expect, "fixture has no needs-you task that unblocks anything"
    row_text = page.evaluate(
        "(id) => { const tr=document.getElementById('ny-row-'+id); return tr ? tr.textContent : ''; }",
        expect["id"],
    )
    assert "unblocks" in row_text, (
        f"needs-you row {expect['id']!r} (expected unblocks {expect['n']}) renders no count: {row_text!r}"
    )


@for_sizes
@pytest.mark.xfail(strict=True, reason="plangraph:UI-FLAKE-FIXES")
def test_v_need_2(page, site):
    """V-NEED-2: the answered filter shows answer, who, when and the D-id.

    The state facet offers 'answered' but the row renders only the question
    text; answer / who / when / D-id (the Q&A history) are never shown.
    """
    _load(page, site)
    _show(page, "needs")
    has_facet = page.evaluate(
        "() => [...document.querySelectorAll('#sb-panel-needs label.tt-fl input')]"
        ".some(i => /^Filter State answered/.test(i.getAttribute('aria-label')||''))"
    )
    assert has_facet, "no 'answered' state facet option"
    page.evaluate(
        "() => { const i=[...document.querySelectorAll('#sb-panel-needs input')]"
        ".find(x => /^Filter State answered/.test(x.getAttribute('aria-label')||'')); if(i) i.click(); }"
    )
    page.wait_for_timeout(150)
    rows = page.evaluate(
        "() => [...document.querySelectorAll('#sb-panel-needs tbody tr')]"
        ".map(tr => [...tr.querySelectorAll('td')].map(td => td.textContent).join(' | '))"
    )
    assert rows, "answered filter returned no rows"
    joined = " || ".join(rows)
    assert "90 cm" in joined and "tess" in joined and "D-11" in joined, (
        f"answered rows lack answer/who/when/D-id: {json.dumps(rows)}"
    )


@for_sizes
@pytest.mark.xfail(strict=True, reason="plangraph:UI-FLAKE-FIXES")
def test_v_need_3(page, site):
    """V-NEED-3: a row's ID link selects the item without reloading the page.

    The ID cell is a bare <a href=...>; clicking it navigates (full reload)
    instead of selecting in place.
    """
    _load(page, site)
    _show(page, "needs")
    page.evaluate("() => { window.__pgSentinel = 'alive'; }")
    link = page.evaluate("() => { const a=document.querySelector('#sb-panel-needs a.ny-id'); return a ? a.id : null; }")
    assert link, "no needs-you ID link"
    page.evaluate("(id) => { document.getElementById(id).querySelector('a.ny-id').click(); }", link)
    page.wait_for_timeout(400)
    sentinel = page.evaluate("() => window.__pgSentinel || 'GONE'")
    assert sentinel == "alive", f"ID link reloaded the page (sentinel {sentinel!r}); spec: no reload"


@for_sizes
def test_v_need_4(page, site, size):
    """V-NEED-4: Jump focuses the item in the graph (V-GRAPH-5: one animation,
    expanded, centred, zoom >= 0.75)."""
    _load(page, site)
    _show(page, "needs")
    target = page.evaluate(
        "() => { const rows=[...document.querySelectorAll('#sb-panel-needs tbody tr')];"
        " for (const r of rows) { const jb=r.querySelector('.ny-jump');"
        " if (jb && !jb.disabled) { jb.click(); return (r.querySelector('.ny-id')||{}).textContent; } }"
        " return null; }"
    )
    assert target, "no enabled Jump button on any needs-you row"
    page.wait_for_timeout(700)
    probes.wait_layout_idle(page)
    st = page.evaluate(
        "() => { const cy=PlanUI.GraphView.getCy(); const sel=PlanPage.state.selected;"
        " let n=cy.getElementById('t:'+sel); if(!n.length) n=cy.getElementById(sel);"
        " if(!n.length) return {page:PlanUI.Pages.current(), sel, found:false};"
        " const b=n.renderedBoundingBox(); return {page:PlanUI.Pages.current(), sel, found:true,"
        " zoom:cy.zoom(), cx:b.x1+b.w/2, cy2:b.y1+b.h/2, vcx:cy.width()/2, vcy:cy.height()/2,"
        " w:b.w, h:b.h}; }"
    )
    assert st["page"] == "graph", f"Jump did not switch to graph (page={st['page']!r})"
    if not st["found"]:
        # V-GRAPH-10: at narrow widths the expanded phase renders no task node yet, so focus has no target here.
        pytest.skip("plangraph:UI-GRAPH-EXPAND: task node absent at this width (V-GRAPH-10)")
    assert st["zoom"] >= 0.75, f"focus zoom {st['zoom']:.2f} below 0.75"
    assert st["w"] > 0 and st["h"] > 0, f"node zero-size after focus: {st['w']}x{st['h']}"
    off = abs(st["cx"] - st["vcx"]) + abs(st["cy2"] - st["vcy"])
    assert off <= 60, f"node not centred, offset {off:.1f}px (node {st['cx']:.0f},{st['cy2']:.0f} vs view {st['vcx']:.0f},{st['vcy']:.0f})"


# ------------------------------------------------------ Section 7: Decisions page
@pytest.mark.xfail(strict=True, reason="plangraph:PG-DEC-ATTRIBUTION")
@for_sizes
def test_v_dec_1(page, site):
    """V-DEC-1: attribute (human/agent) and actor come from keeper decision
    records. No Decisions page or attribute/actor fields exist in the page."""
    raise NotImplementedError("plangraph:PG-DEC-ATTRIBUTION: body lands with the feature")


@pytest.mark.xfail(strict=True, reason="plangraph:PG-DEC-EDIT-NOTICE")
@for_sizes
def test_v_dec_2(page, site):
    """V-DEC-2: an edit shows previous text, editor and time (hash-chained).
    No decision-edit feature exists."""
    raise NotImplementedError("plangraph:PG-DEC-EDIT-NOTICE: body lands with the feature")


@pytest.mark.xfail(strict=True, reason="plangraph:PG-DEC-EDIT-NOTICE")
@for_sizes
def test_v_dec_3(page, site):
    """V-DEC-3: without the operator principal the edit control is not shown.
    There is no edit control or operator principal at all."""
    raise NotImplementedError("plangraph:PG-DEC-EDIT-NOTICE: body lands with the feature")


# ---------------------------------------------------------- Section 8: Activity
@for_sizes
@pytest.mark.xfail(strict=True, reason="plangraph:UI-URL-HISTORY")
def test_v_act_1(page, site):
    """V-ACT-1: the range is in the URL.

    The 24h/7d/30d/custom range is module-local (`mode`), never written to the
    URL, so it is not shareable or reload-stable.
    """
    _load(page, site)
    _show(page, "activity")
    before = page.evaluate("() => location.search + '|' + location.hash")
    page.evaluate(
        "() => { const b=[...document.querySelectorAll('#sb-panel-activity .act-range button')]"
        ".find(x => x.textContent==='30d'); if (b) b.click(); }"
    )
    page.wait_for_timeout(150)
    after = page.evaluate("() => location.search + '|' + location.hash")
    encoded = page.evaluate(
        "() => { const h=location.search + location.hash;"
        " return /(?:^|[?&#])(a_(?:range|start|end|mode)|range|act_range|from|to)=/.test(h); }"
    )
    assert encoded, f"range not encoded in URL after 30d click: search/hash unchanged ({before!r} -> {after!r})"


@for_sizes
@pytest.mark.xfail(strict=True, reason="plangraph:UI-FLAKE-FIXES")
def test_v_act_2(page, site):
    """V-ACT-2: a key link switches to the graph and focuses the item.

    The activity key link calls only PlanPage.selectNode; it does not switch to
    the graph page nor focus/centre the node.
    """
    _load(page, site)
    _show(page, "activity")
    page.evaluate(
        "() => { const b=[...document.querySelectorAll('#sb-panel-activity .act-range button')]"
        ".find(x => x.textContent==='30d'); if (b) b.click(); }"
    )
    page.wait_for_timeout(150)
    link_key = page.evaluate(
        "() => { const l=document.querySelector('#sb-panel-activity .keylink');"
        " if (!l) return null; l.click(); return l.textContent; }"
    )
    assert link_key, "no activity key link in the 30d range"
    page.wait_for_timeout(400)
    st = page.evaluate(
        "() => { const cy=PlanUI.GraphView.getCy(); const sel=PlanPage.state.selected;"
        " let n=cy.getElementById('t:'+sel); if(!n.length) n=cy.getElementById(sel);"
        " let centred=false; if(n.length){ const b=n.renderedBoundingBox();"
        " centred = Math.abs((b.x1+b.w/2)-cy.width()/2)+Math.abs((b.y1+b.h/2)-cy.height()/2) <= 80; }"
        " return {page: PlanUI.Pages.current(), selected: sel, inGraph: n.length>0, centred}; }"
    )
    assert st["page"] == "graph", (
        f"key link did not switch to graph: page={st['page']!r}, selected={st['selected']!r}")
    assert st["inGraph"] and probes.node_in_canvas(page, st["selected"]), (
        f"key link did not focus {st['selected']!r} inside the visible canvas: {st}")


# ---------------------------------------------------------- Section 9: Progress
@for_sizes
@pytest.mark.xfail(strict=True, reason="plangraph:UI-FLAKE-FIXES")
def test_v_prog_1(page, site):
    """V-PROG-1: one counting rule (total = all minus cancelled, done = done) is
    written next to the bar.

    Progress renders the counts but no sentence stating the rule next to the
    bar.
    """
    _load(page, site)
    _show(page, "progress")
    prog = page.evaluate(
        "() => { const box=document.getElementById('sb-panel-progress');"
        " const t=(box.querySelector('.prg-total')||{}).textContent||'';"
        " return {text: t, labels: [...box.querySelectorAll('.prg-pct')].map(s=>s.textContent)}; }"
    )
    assert "33.3%" in prog["text"] and "22/66" in prog["text"], f"unexpected progress total: {prog['text']!r}"
    rule = page.evaluate(
        "() => { const b=document.getElementById('sb-panel-progress');"
        " return /\\bminus\\b|cancel/i.test(b.textContent||''); }"
    )
    assert rule, f"counting rule (total = all minus cancelled) not written next to the bar: {prog['text']!r}"


# --------------------------------------------------------- Section 10: Knowledge
@for_sizes
@pytest.mark.xfail(strict=True, reason="plangraph:UI-FLAKE-FIXES")
def test_v_kg_1(page, site):
    """V-KG-1: with the export missing/disabled, the Knowledge tab stays visible
    and shows its empty state with the reason (V-SHELL-3), and the console has
    no 404 error.

    The fixture serves an enabled:false export (no 404), but the page hides the
    Knowledge tab silently instead of showing the reason.
    """
    kg: list[int] = []
    page.on("response", lambda r: kg.append(r.status) if r.url.endswith("kg-export.json") else None)
    _load(page, site)
    enabled = page.evaluate("() => PlanUI.Knowledge.enabled()")
    assert enabled is False, "fixture export unexpectedly enabled"
    assert not [s for s in kg if s == 404], "kg-export.json 404s on a render without an export"
    hidden = page.evaluate("() => document.getElementById('tab-knowledge').hidden")
    assert hidden is False, "Knowledge tab hidden silently; V-SHELL-3 requires it visible with a reason"


# ------------------------------------------------------------- Section 11: Fleet
@pytest.mark.xfail(strict=True, reason="plangraph:UI-FLEET-TAB")
@for_sizes
def test_v_fleet_1(page, site):
    """V-FLEET-1: until the fleet API exists the Fleet tab shows its empty state
    with the reason. There is no Fleet tab or page in the shell at all."""
    raise NotImplementedError("plangraph:UI-FLEET-TAB: body lands with the feature")


# ---------------------------------------------------------- Section 12: Settings
@for_sizes
def test_v_set_1(page, site):
    """V-SET-1: changing a setting does not reset the graph (no collapse, no
    viewport change)."""
    _load(page, site)
    page.evaluate(  # expand a project so a reset would be observable
        "() => { const vm=PlanUI.app.vm; const p=(vm.projects||[])[0];"
        " if (p) PlanUI.app.ui.set('expanded', new Set([p.name])); }"
    )
    probes.wait_layout_idle(page)
    before = probes.viewport(page)
    expanded_before = page.evaluate("() => [...PlanUI.app.ui.get('expanded')]")
    page.evaluate("() => { document.getElementById('tab-settings').click(); }")
    page.wait_for_timeout(150)
    toggled = page.evaluate(
        "() => { const out=[]; ['set-showCross','set-showAllEdges','set-animate'].forEach(id=>{"
        " const c=document.getElementById(id); if(c){ out.push(id+':'+c.checked); c.click(); } });"
        " return out; }"
    )
    page.wait_for_timeout(300)
    after = probes.viewport(page)
    expanded_after = page.evaluate("() => [...PlanUI.app.ui.get('expanded')]")
    assert toggled, "no settings toggles found"
    assert after["zoom"] == before["zoom"], f"viewport zoom reset: {before['zoom']} -> {after['zoom']}"
    assert after["pan"] == before["pan"], f"viewport pan moved: {before['pan']} -> {after['pan']}"
    assert expanded_after == expanded_before, f"collapse state reset: {expanded_before} -> {expanded_after}"
