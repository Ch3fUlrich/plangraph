"""UI-BROWSER-TESTS (W2c): one test per V-tag for docs/ui/VIEWS.md sections 5-12
(table, needs-you, decisions, activity, progress, knowledge, fleet, settings).

Each test asserts the SPEC against the current page (src/plangraph/web): a tag
whose behaviour is wrong today is ``xfail(strict=True, raises=AssertionError)``
with the owning item; when a fix lane makes it pass, strict xfail reports XPASS
as a failure, so that lane removes the marker, and a harness break (timeout,
KeyError, ...) stays red instead of passing as an expected failure. A tag whose
observable already holds today is an UNMARKED green test (``test_v_dec_3``).
Sizes default to all three SIZES unless the tag names one.
Assertions carry measured probe values.
"""
from __future__ import annotations

import json
import re

import pytest

from browser import probes
from browser.conftest import SIZES, SNAPSHOT_PATH, for_sizes

pytestmark = pytest.mark.browser

#: Fixture snapshot: every expected value in this module comes from HERE,
#: never from the page under test (deepseek review: no circular expectations).
_SNAP = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
_TERMINAL = ("done", "cancelled")


def _show(page, name):
    page.evaluate("(n) => PlanUI.Pages.show(n)", name)
    page.wait_for_timeout(120)


def _load(page, site):
    page.goto(site)
    probes.wait_layout_idle(page)


def _fixture_unblocks(key):
    """V-NEED-1 expected value: the keeper dependency graph, computed from the
    fixture snapshot only (VIEWS.md 6: "unblocks N" = the number of open,
    agent-owned items that wait only on this row, plus the downstream count).

    An item counts once every non-terminal item it waits on has been counted,
    so direct waiters and their downstream come out of one cascade; items that
    wait on something outside the cascade, terminal items and operator-owned
    items never count. Returns None when the snapshot has no such key."""
    items = _SNAP["items"]
    by_key = {i["key"]: i for i in items}
    if key not in by_key:
        return None
    reached, total, grew = {key}, 0, True
    while grew:
        grew = False
        for it in items:
            k = it.get("key")
            if k in reached or it.get("state") in _TERMINAL or it.get("operator"):
                continue
            deps = [d for d in (it.get("depends_on") or [])
                    if d in by_key and by_key[d].get("state") not in _TERMINAL]
            if deps and set(deps) <= reached:
                reached.add(k)
                total += 1
                grew = True
    return total


def _fixture_row_keys():
    """Row id -> snapshot key for every row the needs-you table can render
    (open questions, decisions, operator tasks), straight from the fixture."""
    keys = {q["id"]: q["item"] for q in _SNAP.get("questions", [])}
    keys.update({d["id"]: (d.get("items") or [""])[0]
                 for d in _SNAP.get("decisions", [])})
    keys.update({i["key"]: i["key"] for i in _SNAP["items"]})
    return keys


def _decisions_view(page):
    """User path to the Decisions view (VIEWS.md 7): a tab or menu control
    named for it. Returns the control's accessible name or None, then the
    state of #decisions-table."""
    ctl = page.evaluate(
        """() => { const els = [...document.querySelectorAll(
             'header .tab-btn, header button, .sb-menu button, .sb-tab')];
           const b = els.find(e => /decision/i.test(
             (e.getAttribute('aria-label') || '') + ' ' + (e.textContent || '')));
           if (b) b.click();
           return b ? String(b.getAttribute('aria-label') || b.textContent || '').trim() : null; }"""
    )
    page.wait_for_timeout(200)
    st = page.evaluate(
        """() => { const t = document.getElementById('decisions-table');
           const p = document.getElementById('panel-qa');
           return {exists: !!t, visible: !!t && t.getClientRects().length > 0,
                   panelHidden: p ? p.hidden : null,
                   cols: t ? [...t.querySelectorAll('thead th')].map(h => h.textContent.trim()) : []}; }"""
    )
    return ctl, st


def _decision_cells(page):
    """{decision id: {attribute: ..., actor: ...}} from the decisions table."""
    return page.evaluate(
        """() => { const t = document.getElementById('decisions-table');
           if (!t) return null;
           const heads = [...t.querySelectorAll('thead th')].map(h => h.textContent.trim());
           const idx = n => heads.findIndex(h => h.toLowerCase().indexOf(n) >= 0);
           const out = {};
           [...t.querySelectorAll('tbody tr')].forEach(tr => {
             const c = [...tr.children];
             out[(tr.id.replace(/^qa-row-/, '')) || (c[0] ? c[0].textContent.trim() : '')] = {
               attribute: idx('attribute') >= 0 && c[idx('attribute')] ? c[idx('attribute')].textContent.trim() : null,
               actor: idx('actor') >= 0 && c[idx('actor')] ? c[idx('actor')].textContent.trim() : null};
           });
           return {rows: out, heads}; }"""
    )


# ---------------------------------------------------------------- Section 5: Table
@for_sizes
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:UI-FLAKE-FIXES")
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
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:UI-URL-HISTORY")
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
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:UI-FLAKE-FIXES")
def test_v_need_1(page, site):
    """V-NEED-1: the unblocks count equals the keeper's dependency graph
    (tested against the fixture snapshot, never against the page's own VM).

    Every expected number comes from ``_fixture_unblocks`` (fixture JSON); the
    page is only read for what it renders. The needs-you table has no
    unblocks column at all (columns: Type/ID/Title/Project/Prio/State/Age/Open)."""
    _load(page, site)
    _show(page, "needs")
    expected = {rid: _fixture_unblocks(key)
                for rid, key in _fixture_row_keys().items()}
    row_ids = page.evaluate(
        "() => [...document.querySelectorAll('#sb-panel-needs tbody tr')]"
        ".map(t => t.id.replace(/^ny-row-/, ''))")
    assert row_ids, "needs-you table rendered no rows"
    unlisted = [r for r in row_ids if r not in expected]
    assert not unlisted, (
        "rendered rows with no fixture record behind them: %r" % unlisted)
    assert any(expected[r] for r in row_ids), (
        "fixture dependency graph unblocks nothing on any rendered row: %r"
        % {r: expected[r] for r in row_ids})
    heads = page.evaluate(
        "() => [...document.querySelectorAll('#sb-panel-needs thead th')]"
        ".map(t => t.textContent.trim())")
    col = next((i for i, h in enumerate(heads) if "unblock" in h.lower()), -1)
    assert col >= 0, (
        "needs-you table has no 'unblocks' column (columns %r); VIEWS.md 6 "
        "requires \"unblocks N\" per row" % heads)
    got = page.evaluate(
        "(c) => { const out = {}; document.querySelectorAll("
        "'#sb-panel-needs tbody tr').forEach(tr => { const td = tr.children[c];"
        " out[tr.id.replace(/^ny-row-/, '')] = td ? td.textContent.trim() : null; });"
        " return out; }", col)
    wrong = {}
    for rid in row_ids:
        m = re.match(r"\s*(\d+)", got.get(rid) or "")
        want = expected[rid]
        if not m or int(m.group(1)) != want:
            wrong[rid] = (got.get(rid), want)
    assert not wrong, (
        "rows whose unblocks count disagrees with the fixture dependency "
        "graph {(shown, expected)}: %r" % wrong)


@for_sizes
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:UI-FLAKE-FIXES")
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
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:UI-URL-HISTORY")
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


@pytest.mark.parametrize("size", [
    pytest.param(SIZES["desktop"], id="desktop"),
    pytest.param(SIZES["tablet"], id="tablet"),
    # At 390x844 the expanded phase renders no focus target, so Jump cannot
    # centre anything: that is the UI-GRAPH-EXPAND defect (V-GRAPH-5/V-GRAPH-10),
    # asserted - never skipped - and strict, so the lane turns it green.
    pytest.param(SIZES["phone"], id="phone",
                 marks=pytest.mark.xfail(strict=True, raises=AssertionError,
                                         reason="plangraph:UI-GRAPH-EXPAND")),
], indirect=True)
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
    assert st["found"], (
        "Jump selected %r but no node for it exists in the graph (viewport "
        "%dx%d), so the focus/centre/zoom check has no target: %r"
        % (st["sel"], size[0], size[1], st))
    assert st["zoom"] >= 0.75, f"focus zoom {st['zoom']:.2f} below 0.75"
    assert st["w"] > 0 and st["h"] > 0, f"node zero-size after focus: {st['w']}x{st['h']}"
    off = abs(st["cx"] - st["vcx"]) + abs(st["cy2"] - st["vcy"])
    assert off <= 60, f"node not centred, offset {off:.1f}px (node {st['cx']:.0f},{st['cy2']:.0f} vs view {st['vcx']:.0f},{st['vcy']:.0f})"


# ------------------------------------------------------ Section 7: Decisions page
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:PG-DEC-ATTRIBUTION")
@for_sizes
def test_v_dec_1(page, site):
    """V-DEC-1: attribute (human/agent) and actor come from the keeper decision
    records of the snapshot, not from a heuristic in the page.

    Opens the Decisions view the user way (a tab named for it), then compares
    every rendered row with ``tests/fixtures/ui_snapshot.json``."""
    _load(page, site)
    ctl, st = _decisions_view(page)
    assert ctl, (
        "no control opens the Decisions view (VIEWS.md 7 'one row per D-row'); "
        "#decisions-table state %r" % st)
    assert st["exists"] and st["visible"], (
        "the Decisions view is not on screen after opening it: %r" % st)
    cells = _decision_cells(page)
    assert cells, "no decisions table to read attribute/actor from"
    heads = [h.lower() for h in cells["heads"]]
    for field in ("attribute", "actor"):
        assert any(field in h for h in heads), (
            "decisions table has no %r column (columns %r); VIEWS.md 7 shows "
            "attribute human|agent and actor per row" % (field, cells["heads"]))
    wrong = {}
    for d in _SNAP["decisions"]:
        rec = cells["rows"].get(d["id"])
        if rec is None:
            wrong[d["id"]] = {"row": None}
            continue
        exp_attr, exp_actor = d.get("attribute"), d.get("actor")
        if rec["attribute"] not in ("human", "agent"):
            wrong[d["id"]] = {"attribute": rec["attribute"], "keeper": exp_attr}
        elif rec["attribute"] != exp_attr or (rec["actor"] or None) != exp_actor:
            wrong[d["id"]] = {"shown": {"attribute": rec["attribute"],
                                        "actor": rec["actor"]},
                              "keeper": {"attribute": exp_attr, "actor": exp_actor}}
    assert not wrong, (
        "rows whose attribute/actor is not the keeper record's "
        "(attribute must be human|agent, both taken from the record): %r" % wrong)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:PG-DEC-EDIT-NOTICE")
@for_sizes
def test_v_dec_2(page, site):
    """V-DEC-2: an edit shows the previous text, the editor and the time
    (hash-chained event), so the Decisions view needs an edit control and a
    history surface. Neither exists.

    The history check runs FIRST and is not vacuous: the fixture carries an
    edited decision (D-13 with a ``history`` entry, see
    ``test_fixture_decisions_include_an_edited_record``), and its previous
    text / editor / time must literally appear in the decisions surface.
    """
    _load(page, site)
    ctl, st = _decisions_view(page)
    edited = [d for d in _SNAP["decisions"]
              if d.get("history") or d.get("edited")]
    assert edited, (
        "the fixture carries no edited decision, so V-DEC-2's previous text/"
        "editor/time cannot be asserted (guard: "
        "test_fixture_decisions_include_an_edited_record)")
    surface = page.evaluate(
        """() => { const p = document.getElementById('panel-qa');
           return p ? String(p.textContent || '') : ''; }"""
    )
    assert surface.strip(), (
        "the decisions surface is empty, it can show no edit history: "
        "control=%r view=%r surface=%r" % (ctl, st, surface[:120]))
    missing: dict[str, list] = {}
    for d in edited:
        for ev in (d.get("history") or []):
            for field in ("previous_text", "editor", "time"):
                val = ev.get(field)
                if not val or str(val) not in surface:
                    missing.setdefault(d["id"], []).append({field: val})
    assert not missing, (
        "decisions whose edit history is NOT shown in the Decisions view "
        "(expected {decision: [{field: value}]} - V-DEC-2 requires the previous "
        "text, the editor and the time of the hash-chained event): %r" % missing)
    edit = page.evaluate(
        """() => [...document.querySelectorAll('#decisions-table tbody button, #decisions-table tbody a')]
             .map(b => String(b.getAttribute('aria-label') || b.title || b.textContent || '').trim())
             .filter(t => /\\bedit\\b/i.test(t))"""
    )
    assert edit, (
        "the Decisions view offers no edit control, so no edit (previous "
        "text, editor, time) can be shown: control=%r view=%r" % (ctl, st))


def test_fixture_decisions_include_an_edited_record():
    """V-DEC-2 coverage guard (unmarked, must stay green): the fixture really
    carries an edited decision whose history names the previous text, the editor
    and the time, so ``test_v_dec_2`` can never pass vacuously on a snapshot
    without history. Fixture source: ``tools/gen_ui_snapshot.py`` (D-13)."""
    edited = [d for d in _SNAP["decisions"]
              if d.get("history") or d.get("edited")]
    assert edited, (
        "tests/fixtures/ui_snapshot.json has no edited decision, so the "
        "V-DEC-2 history assertion has nothing to compare: decisions=%r"
        % [d.get("id") for d in _SNAP["decisions"]])
    for d in edited:
        events = d.get("history") or []
        assert events, "edited decision %s carries no history event" % d["id"]
        for ev in events:
            for field in ("previous_text", "editor", "time"):
                assert ev.get(field), (
                    "decision %s history event lacks %r: %r" % (d["id"], field, ev))


@for_sizes
def test_v_dec_3(page, site):
    """V-DEC-3: without the operator principal, the edit control is not shown.

    No marker: V-DEC-3's own observable already holds today - this session has
    no operator principal and no edit control exists - so the test is green and
    stays green until an edit control appears that is not gated on a principal.

    The page's missing principal STATE is a different assertion and a different
    owner: ``test_v_dec_3_principal_state`` below, strict xfail for
    ``plangraph:PG-ANSWER-UI``, the lane the plan names for the write identity
    (docs/plans/ui/2026-10-17-ui-plan.yaml line 249 "the write identity is the
    operator principal from PG-ANSWER-UI", depends_on line 252; plan section 5,
    docs/plans/ui/2026-10-17-ui-plan.md line 86: PG-ANSWER-UI "brings the
    operator principal through the proxy hop").
    """
    _load(page, site)
    _assert_no_decision_edit_control(page)


def _assert_no_decision_edit_control(page):
    """Question answer editing must not count as decision editing."""
    st = page.evaluate(
        """() => { const p = document.getElementById('panel-qa');
           const t = document.getElementById('decisions-table');
           return {panel: !!p, table: !!t,
                   rows: t ? t.querySelectorAll('tbody tr').length : 0}; }"""
    )
    assert st["panel"] and st["table"], (
        "the Decisions surface is gone, V-DEC-3 has nothing to check against "
        "(a decision row is where an edit control must be withheld): %r" % st)
    assert st["rows"] == len(_SNAP["decisions"]), (
        "the decisions surface does not render the fixture's decisions, so the "
        "edit-control scan would be vacuous: rows=%d, fixture=%d"
        % (st["rows"], len(_SNAP["decisions"])))
    edit = page.evaluate(
        """() => [...document.querySelectorAll('#decisions-table tbody button, #decisions-table tbody a')]
             .map(b => String(b.getAttribute('aria-label') || b.title || b.textContent || '').trim())
             .filter(t => /\\bedit\\b/i.test(t))"""
    )
    assert not edit, (
        "edit control shown without an operator principal: %r" % edit)


@for_sizes
def test_v_dec_3_with_local_question_answer(page, site):
    """A question's local-answer Edit control is outside V-DEC-3's scope."""
    _load(page, site)
    qid = _SNAP["questions"][0]["id"]
    page.evaluate("(id) => PlanUI.QAPage.setAnswer(id, 'Scope regression answer')", qid)
    question_edit = page.locator("#qa-table").get_by_role(
        "button", name="Edit", exact=True, include_hidden=True)
    assert question_edit.count() == 1, "seeded local answer rendered no question Edit button"
    _assert_no_decision_edit_control(page)


@for_sizes
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:PG-ANSWER-UI")
def test_v_dec_3_principal_state(page, site):
    """V-DEC-3's precondition, split out of ``test_v_dec_3`` (deepseek review:
    that test must go red - or stay green - on V-DEC-3's own observable, not on
    an internal principal-store key).

    The page must keep operator-principal state to gate an edit control on.
    Owner ``plangraph:PG-ANSWER-UI``: the plan makes it the source of the write
    identity (docs/plans/ui/2026-10-17-ui-plan.yaml line 249 "the write identity
    is the operator principal from PG-ANSWER-UI", depends_on line 252; plan
    section 5, docs/plans/ui/2026-10-17-ui-plan.md lines 86-87), and VIEWS.md 6
    (docs/ui/VIEWS.md line 111) names plangraph:PG-ANSWER-UI for the operator
    write path. PG-DEC-EDIT-NOTICE (the old marker) owns the edit flow, not the
    principal."""
    _load(page, site)
    principal = page.evaluate(
        """() => { const ui = window.PlanUI && PlanUI.app && PlanUI.app.ui;
           if (!ui || typeof ui.get !== 'function') return null;
           for (const k of ['principal', 'identity', 'role', 'actor', 'who']) {
             let v; try { v = ui.get(k); } catch (e) { continue; }
             if (v !== undefined && v !== null && v !== '') return {key: k, value: v};
           }
           return null; }"""
    )
    assert principal is not None, (
        "the page keeps no principal/identity state to gate editing on "
        "(checked store keys principal/identity/role/actor/who): principal="
        "%r - V-DEC-3's operator-only gate cannot exist until PG-ANSWER-UI "
        "supplies the operator principal" % (principal,))


# ---------------------------------------------------------- Section 8: Activity
@for_sizes
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:UI-URL-HISTORY")
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
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:UI-FLAKE-FIXES")
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
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:UI-FLAKE-FIXES")
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
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:UI-FLAKE-FIXES")
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
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:UI-FLEET-TAB")
@for_sizes
def test_v_fleet_1(page, site):
    """V-FLEET-1: until the fleet API exists the Fleet tab shows its empty
    state with the reason. The shell has no Fleet tab at all, so the empty
    state can never be reached.

    Owner ``plangraph:UI-FLEET-TAB``, the lane that builds the tab:
    docs/plans/ui/2026-10-17-ui-plan.yaml lines 253-254 (``op: edit`` /
    ``key: plangraph:UI-FLEET-TAB``) with done criteria lines 256-257 ("browser
    test; per docs/ui/VIEWS.md section 11"), plan section 5
    (docs/plans/ui/2026-10-17-ui-plan.md line 88: "UI-FLEET-TAB: done criteria
    point at VIEWS.md 11") and docs/ui/VIEWS.md line 149 ("Built by
    plangraph:UI-FLEET-TAB"). UI-FLAKE-FIXES (yaml line 161) also lists
    V-FLEET-1 in its done criteria; the section-11 builder named by the plan
    and the spec is UI-FLEET-TAB."""
    _load(page, site)
    tab = page.evaluate(
        """() => { const els = [...document.querySelectorAll(
             'header .tab-btn, header button, .sb-menu button')];
           const b = els.find(e => /fleet/i.test(
             (e.getAttribute('aria-label') || '') + ' ' + (e.textContent || '')));
           if (b) b.click();
           return b ? String(b.getAttribute('aria-label') || b.textContent || '').trim() : null; }"""
    )
    assert tab, (
        "no Fleet tab in the shell; VIEWS.md 11 requires the tab (with its "
        "empty state) until the fleet API exists")
    page.wait_for_timeout(200)
    panel = page.evaluate(
        """() => { const p = document.querySelector(
             '[id*=fleet][role=tabpanel], [id*=fleet].tabpanel');
           return p ? {hidden: p.hidden, text: (p.textContent || '').trim()} : null; }"""
    )
    assert panel and not panel["hidden"], (
        "the Fleet tab does not show its panel: %r" % panel)
    assert panel["text"], "the Fleet panel is blank, it shows no empty state"
    assert re.search(r"no |not |until |missing|unavailable|empty", panel["text"], re.I), (
        "the Fleet empty state gives no reason (VIEWS.md 11: the tab shows "
        "its empty state with the reason): %r" % panel["text"])


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
