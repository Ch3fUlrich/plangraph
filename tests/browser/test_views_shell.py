"""VIEWS.md V-tags for the shell: V-ALL-1..7, V-SHELL-1..4, V-SIDE-1..5, V-DET-1..5.
Tests assert the SPEC, not current behaviour: failing code = strict xfail tagged
with the owning card; when a fix lane makes it pass, strict xfail reports XPASS
as a failure, so that lane removes the marker. Selections go through Table rows
(.tc-open -> GraphView.selectNode) so tests never trigger a reveal/relayout by
accident.
"""
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlparse

import pytest
from browser import probes
from browser.conftest import SIZES, SNAPSHOT_PATH, _allowed_warning_res
from plangraph.render import build_sha

pytestmark = pytest.mark.browser
SEL_KEY = "orchard:OR-3"          # open lane, phase orchard|T2
DEP_KEY = "lighthouse:LH-3"       # its cross-project dep; lighthouse|T2 is collapsed
_SNAP = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

def _sizes(xfail=None):
    out = []
    for name, val in SIZES.items():
        r = xfail if isinstance(xfail, str) else (xfail or {}).get(name)
        marks = [pytest.mark.xfail(strict=True, raises=AssertionError, reason="plangraph:" + r)] if r else ()
        out.append(pytest.param(val, id=name, marks=marks))
    return out

def _goto(page, site): page.goto(site); probes.wait_layout_idle(page)

def _click(page, sel):
    ok = page.evaluate("(s) => { const el = document.querySelector(s); if (!el) return false; el.click(); return true; }", sel)
    assert ok, "missing element " + sel
    page.wait_for_timeout(120)

def _nav(page, name):
    _click(page, "#tab-" + ("list" if name == "table" else name))

def _click_row(page, key):
    _click(page, '#panel-list tr[data-key="%s"] .tc-open' % key)

def _select(page, key): _nav(page, "table"); _click_row(page, key)

def _sel(page): return page.evaluate("() => PlanUI.app.ui.get('sel')")

def _view(page): return page.evaluate("() => PlanUI.app.ui.get('view')")

def _h2(page):
    return page.evaluate("() => { const h = document.querySelector('#details h2'); return h ? h.textContent : null; }")

def _hidden(page): return page.evaluate("() => document.getElementById('details').hidden")

def _cy_len(page, node_id):
    return page.evaluate("(i) => { const c = PlanUI.GraphView.getCy(); return c ? c.getElementById(i).length : -1; }", node_id)

@contextmanager
def _own_page(browser, site, size, vendor_assets):
    """Page with its own console capture (no shared teardown) for tests that may log."""
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]})
    pg = ctx.new_page()
    seen, errs, stray = [], [], []
    pg.on("console", lambda m: seen.append((m.type, m.text)))
    pg.on("pageerror", lambda e: errs.append(str(e)))
    def _route(route):
        u = route.request.url
        if urlparse(u).hostname == "127.0.0.1":
            if u.endswith("/favicon.ico"):
                route.fulfill(status=204, content_type="image/x-icon", body=b"")
            else:
                route.continue_()
        elif u in vendor_assets:
            route.fulfill(status=200, content_type="application/javascript",
                          body=vendor_assets[u].read_bytes())
        else:
            stray.append(u)
            route.abort()
    pg.route("**/*", _route)
    try:
        yield pg, seen, errs, stray
    finally:
        ctx.close()

def _console_bad(seen):
    allow = _allowed_warning_res()
    return [t for typ, t in seen if typ == "error" or (typ in ("warning", "warn") and not any(r.search(t) for r in allow))]

_EYE_HARBOR = ("() => { const r = [...document.querySelectorAll('#project-tree"
               " .pt-project-row')].find(x => x.textContent.includes('harbor'));"
               " r.parentElement.querySelector('.pt-eye').click(); }")
@pytest.mark.parametrize("size", _sizes(), indirect=True)
def test_v_all_1(browser, site, size, vendor_assets):
    """V-ALL-1: routine interactions leave a clean console."""
    with _own_page(browser, site, size, vendor_assets) as (pg, seen, errs, stray):
        pg.goto(site); probes.wait_layout_idle(pg)
        base = len(seen)
        for tab in ("table", "needs", "activity", "progress", "settings"):
            pg.evaluate("(id) => document.getElementById(id).click()",
                        "tab-" + ("list" if tab == "table" else tab))
        pg.evaluate("(s) => document.querySelector(s).click()",
                    '#panel-list tr[data-key="%s"] .tc-open' % SEL_KEY)
        pg.evaluate("() => document.activeElement && document.activeElement.blur()")
        pg.keyboard.press("Escape")
        pg.evaluate(_EYE_HARBOR); probes.wait_layout_idle(pg)
        pg.evaluate(_EYE_HARBOR); probes.wait_layout_idle(pg)
        pg.evaluate("() => { const b = document.querySelector('#project-tree .pt-project-row'); b.click(); b.click(); }")
        pg.evaluate("() => document.getElementById('tab-graph').click()")
        probes.wait_layout_idle(pg)
        pg.wait_for_timeout(300)
        bad = _console_bad(seen[base:])
        assert not bad and not errs and not stray, \
            "console: bad=%r pageerrors=%r stray=%r" % (bad[:3], errs[:1], stray[:1])

@pytest.mark.parametrize("size", _sizes(), indirect=True)
def test_v_all_2(page, site, size):
    """V-ALL-2: every reachable page shows text, never a blank panel."""
    _goto(page, site)
    panels = {"graph": "panel-graph", "table": "panel-list", "needs": "panel-needs",
              "activity": "panel-activity", "progress": "panel-progress", "settings": "panel-settings"}
    got = {}
    for name, pid in panels.items():
        _nav(page, name)
        got[name] = page.evaluate(
            "(i) => document.getElementById(i).textContent.trim().length", pid)
    assert all(n > 0 for n in got.values()), "visible text lengths: %r" % got

@pytest.mark.parametrize("size", _sizes(), indirect=True)
def test_v_all_3(page, site, size):
    """V-ALL-3: item text renders as text, complete."""
    _goto(page, site)
    _nav(page, "table")
    titles = [i["title"] for i in _SNAP["items"] if i["title"]]
    found = page.evaluate(
        "(ts) => { const t = document.getElementById('panel-list').textContent;"
        " return ts.filter(x => t.indexOf(x) >= 0).length; }", titles)
    junk = page.evaluate("() => document.getElementById('panel-list')"
                         ".textContent.match(/\\[object Object\\]|undefined/g)")
    assert found == len(titles), "titles found %d/%d" % (found, len(titles))
    assert not junk, "junk tokens in table: %r" % (junk[:3],)

@pytest.mark.parametrize("size", _sizes(), indirect=True)
def test_v_all_4(page, site, size):
    """V-ALL-4: the URL reproduces view + selection after reload."""
    _goto(page, site)
    _select(page, SEL_KEY)
    hash_a = page.evaluate("() => location.hash")
    assert "sel=" in hash_a, "hash after select: %r" % hash_a
    page.reload()
    probes.wait_layout_idle(page)
    assert _sel(page) == SEL_KEY, "sel after reload: %r" % _sel(page)
    assert _view(page) == "table", "view after reload: %r" % _view(page)
    assert _h2(page) == SEL_KEY, "details h2 after reload: %r" % _h2(page)

@pytest.mark.parametrize("size", _sizes("UI-URL-HISTORY"), indirect=True)
def test_v_all_5(page, site, size):
    """V-ALL-5: selecting creates a history entry (back must restore state)."""
    _goto(page, site)
    _nav(page, "table")
    len0 = page.evaluate("() => history.length")
    for key in (SEL_KEY, "orchard:OR-4"): _click_row(page, key)
    len1 = page.evaluate("() => history.length")
    assert len1 >= len0 + 2, "history entries %d -> %d for 2 selections" % (len0, len1)

@pytest.mark.parametrize("size", [pytest.param(SIZES["phone"], id="phone",
    marks=pytest.mark.xfail(strict=True, raises=AssertionError,
                            reason="plangraph:UI-NARROW-LAYOUT"))], indirect=True)
def test_v_all_6(page, site, size):
    """V-ALL-6: touch targets >= 40 px at 390 px wide, no horizontal overflow."""
    _goto(page, site)
    _select(page, SEL_KEY)
    m = page.evaluate("""() => { const out = [];
      const add = (n, el) => { if (el && el.getClientRects().length) {
        const r = el.getBoundingClientRect(); out.push({n, w: r.width, h: r.height}); } };
      document.querySelectorAll('#tabbar .tab-btn').forEach((b, i) => add('tabbar' + i, b));
      add('search-toggle', document.getElementById('search-toggle'));
      add('details-close', document.querySelector('#details .details-close'));
      return {items: out, scrollW: document.documentElement.scrollWidth,
              vw: window.innerWidth}; }""")
    small = [i for i in m["items"] if i["w"] < 40 or i["h"] < 40]
    assert not small, "undersized targets at 390px: %r" % small
    assert m["scrollW"] <= m["vw"], \
        "horizontal overflow: scrollWidth %d > viewport %d" % (m["scrollW"], m["vw"])

@pytest.mark.parametrize("size", _sizes("UI-FLAKE-FIXES"), indirect=True)
def test_v_all_7(browser, site, size, vendor_assets):
    """V-ALL-7: keyboard shortcuts act only on the visible page."""
    with _own_page(browser, site, size, vendor_assets) as (pg, seen, errs, stray):
        pg.goto(site); probes.wait_layout_idle(pg)
        pg.evaluate("() => document.getElementById('tab-progress').click()")
        pg.evaluate("() => document.activeElement && document.activeElement.blur()")
        assert pg.evaluate("() => PlanUI.app.ui.get('sel')") is None
        pg.keyboard.press("n")
        pg.wait_for_timeout(200)
        sel = pg.evaluate("() => PlanUI.app.ui.get('sel')")
        assert sel is None, \
            "'n' on the Progress page selected %r while the graph is hidden" % sel
        # the shortcut run must also stay inside the console budget (V-ALL-1)
        bad = _console_bad(seen)
        assert not bad and not errs and not stray, \
            "console: bad=%r pageerrors=%r stray=%r" % (bad[:3], errs[:1], stray[:1])

@pytest.mark.parametrize("size", _sizes(), indirect=True)
def test_v_shell_1(page, site, size):
    """V-SHELL-1: build stamp with the renderer's git code sha, the snapshot time
    and the exact ``head_seq``, and the same sha in a ``<meta name=plangraph-build>``
    tag. The code sha is compared byte for byte with ``render.build_sha()`` (the
    renderer git sha) - never with a hex-looking token, which would also match the
    keeper head hash the spec excludes."""
    _goto(page, site)
    meta = page.evaluate("() => { const m ="
        " document.querySelector('meta[name=\"plangraph-build\"]');"
        " return m ? m.content : null; }")
    stamp = (page.text_content("#meta-line") or "").strip()
    assert meta is not None, "no <meta name=plangraph-build>; stamp text %r" % stamp
    want_sha = build_sha()            # what render.py stamps (UI-VERSION-STAMP)
    m = re.match(r"^code (\S+) \u00b7 data (\S+) \u00b7 seq (\S+)", stamp)
    assert m, "stamp does not start 'code <sha> \u00b7 data <time> \u00b7 seq <n>': %r" % stamp
    code, data, seq = m.groups()
    assert code == want_sha, \
        "stamp code sha %r is not the renderer git sha %r" % (code, want_sha)
    assert meta == want_sha, \
        "meta plangraph-build %r is not the renderer git sha %r" % (meta, want_sha)
    assert data == _SNAP["generated"], \
        "snapshot time %r != fixture generated %r" % (data, _SNAP["generated"])
    assert seq == str(_SNAP["head_seq"]), \
        "head_seq %r != fixture head_seq %r (stamp %r)" % (seq, _SNAP["head_seq"], stamp)
    head = _SNAP.get("head")
    assert head is None or code != str(head)[:len(code)], \
        "code sha %r is the keeper head hash %r, not the renderer git sha" % (code, head)

@pytest.mark.parametrize("size", _sizes("UI-NARROW-LAYOUT"), indirect=True)
def test_v_shell_2(page, site, size):
    """V-SHELL-2: the header stays constant across pages (no reflow)."""
    _goto(page, site)
    heights = {}
    for name in ("graph", "table", "needs", "activity", "progress", "settings"):
        _nav(page, name)
        heights[name] = page.evaluate("() => document.querySelector('.top').getBoundingClientRect().height")
    spread = max(heights.values()) - min(heights.values())
    assert spread <= 1, "header heights per page: %r (spread %.1f px)" % (heights, spread)

@pytest.mark.parametrize("size", _sizes("UI-FLAKE-FIXES"), indirect=True)
def test_v_shell_3(page, site, size):
    """V-SHELL-3: a tab whose data is missing stays visible with a reason."""
    _goto(page, site)
    hidden = page.evaluate("() => document.getElementById('tab-knowledge').hidden")
    assert not hidden, "Knowledge tab hidden (no data); spec: visible with reason"

def _stamp_age_minutes(stamp):
    """Age in minutes the stamp states ('... · age 42 min' or '... · age 305h 12m'),
    or None when the stamp states no age at all."""
    m = re.search(r"\u00b7 age (\d+) min", stamp)
    if m:
        return int(m.group(1))
    m = re.search(r"\u00b7 age (\d+)h (\d+)m", stamp)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


@pytest.mark.parametrize("size", _sizes(), indirect=True)
def test_v_shell_4(page, site, size):
    """V-SHELL-4: the data age is shown, and a snapshot older than 15 min turns the
    stamp amber *and* says why (the stated age has to be the snapshot's real age,
    and the amber class has to change the rendered colour)."""
    generated = _SNAP["generated"]
    try:
        gen_ms = datetime.fromisoformat(str(generated).replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        pytest.fail("snapshot generated %r is not an ISO timestamp, V-SHELL-4 "
                    "cannot compute the data age" % generated)
    t0 = time.time() * 1000
    _goto(page, site)
    stamp = (page.text_content("#meta-line") or "").strip()
    cls = page.evaluate("() => document.getElementById('meta-line').className")
    colour = page.evaluate(
        "() => getComputedStyle(document.getElementById('meta-line')).color")
    t1 = time.time() * 1000
    lo, hi = int((t0 - gen_ms) // 60000), int((t1 - gen_ms) // 60000)
    assert lo > 15, (
        "fixture snapshot %s is %d min old, the >15 min clause is not exercised"
        % (generated, lo))
    shown = _stamp_age_minutes(stamp)
    assert shown is not None, (
        "snapshot is %d-%d min old (>>15 min) but the stamp states no age: %r"
        % (lo, hi, stamp))
    assert lo <= shown <= hi, (
        "stamp states an age of %d min, the snapshot was %d-%d min old when the "
        "page rendered: %r" % (shown, lo, hi, stamp))
    assert "stamp-stale" in cls.split(), (
        "snapshot is %d min old but #meta-line carries no stamp-stale (amber) "
        "class: className %r, stamp %r" % (lo, cls, stamp))
    fresh = page.evaluate(
        "() => { const e = document.getElementById('meta-line');"
        " e.classList.remove('stamp-stale');"
        " return getComputedStyle(e).color; }")
    assert colour != fresh, (
        "stamp-stale does not turn the stamp amber: stale colour %s, colour "
        "without the class %s (stamp %r)" % (colour, fresh, stamp))

@pytest.mark.parametrize("size", _sizes({"tablet": "UI-NARROW-LAYOUT"}), indirect=True)
def test_v_side_1(page, site, size):
    """V-SIDE-1: below 1200 px the sidebar starts closed; no scrim steals clicks."""
    w, _h = size
    if w >= 1200:
        pytest.skip("V-SIDE-1 clause applies below 1200 px only")
    _goto(page, site)
    sbw = page.evaluate("() => document.getElementById('sidebar').offsetWidth")
    scrim = page.evaluate("() => { const s = document.getElementById('sb-scrim');"
                          " return !s.hidden && s.getClientRects().length > 0; }")
    canvas = probes.canvas_rect(page)
    hit = page.evaluate("(p) => { const el = document.elementFromPoint(p.x, p.y);"
                        " if (!el) return null; return el.id || (el.closest('#cy') ? 'cy' : el.tagName); }",
                        {"x": canvas["x"] + canvas["width"] / 2, "y": canvas["y"] + canvas["height"] / 2})
    assert sbw == 0, "sidebar open at load: %d px wide" % sbw
    assert not scrim, "scrim visible at load, stealing the first click"
    assert hit == "cy", "canvas centre click would hit %r, not the graph" % hit

@pytest.mark.parametrize("size", _sizes({"tablet": "UI-NARROW-LAYOUT"}), indirect=True)
def test_v_side_2(page, site, size):
    """V-SIDE-2: opening the details panel never collapses or hides the tree."""
    if size == SIZES["phone"]:
        pytest.skip("tree lives in the phone menu drawer; clause n/a there")
    _goto(page, site)
    probe = ("() => [document.getElementById('sidebar').offsetWidth,"
             " document.querySelector('.sb-body').clientHeight]")
    _nav(page, "table")
    before = page.evaluate(probe)
    _click_row(page, SEL_KEY)
    after = page.evaluate(probe)
    assert after == before, \
        "sidebar/tree changed when details opened: before=%r after=%r px" % (before, after)

@pytest.mark.parametrize("size", _sizes("UI-FLAKE-FIXES"), indirect=True)
def test_v_side_3(page, site, size):
    """V-SIDE-3: the tree marks and scrolls to the selected task."""
    _goto(page, site)
    _select(page, SEL_KEY)
    page.evaluate("() => { [...document.querySelectorAll('#project-tree"
                  " .pt-project-row')].find(x => x.textContent.includes('orchard'))"
                  ".click(); }")
    page.evaluate("() => { const li = [...document.querySelectorAll('#project-tree"
                  " .pt-project')].find(x => x.textContent.includes('orchard'));"
                  " [...li.querySelectorAll('.pt-phase-row')]"
                  ".find(x => x.textContent.includes('T2')).click(); }")
    marked = page.evaluate(
        "() => document.querySelectorAll('#project-tree [aria-selected=\"true\"],"
        " #project-tree .selected, #project-tree [data-selected]').length")
    assert marked >= 1, "tree rows marking the selection: %d (expected 1)" % marked

def _harbor_nodes(page):
    """Every cytoscape node id that belongs to the harbor project (p:/ph:/pc:/phc:/t:)."""
    return page.evaluate(
        "() => { const cy = PlanUI.GraphView.getCy(); if (!cy) return [];"
        " return cy.nodes().map(n => n.id()).filter(i => i.indexOf('harbor') >= 0); }")


@pytest.mark.parametrize("size", _sizes("UI-URL-HISTORY"), indirect=True)
def test_v_side_4(page, site, size):
    """V-SIDE-4: eye state survives reload and the tree agrees with the graph.

    The graph opens on the collapsed project overview, so ``p:harbor`` does not
    exist until harbor is expanded: the toggle is asserted on a node that was
    actually drawn, and its disappearance is checked against the nodes that were
    there before (an empty lookup would pass with a broken eye)."""
    _goto(page, site)
    probes.click_node(page, "pc:harbor")          # draw harbor, then toggle its eye
    probes.wait_layout_idle(page)
    drawn = _harbor_nodes(page)
    assert "p:harbor" in drawn, \
        "harbor never expanded, there is nothing to toggle: %r" % sorted(drawn)
    others_before = _cy_len(page, "pc:lighthouse") + _cy_len(page, "p:lighthouse")
    assert others_before > 0, "precondition: no lighthouse card drawn to compare against"
    page.evaluate(_EYE_HARBOR)
    probes.wait_layout_idle(page)
    hash_a = page.evaluate("() => location.hash")
    assert "fpr=" in hash_a, "filter state not in URL: %r" % hash_a
    left = _harbor_nodes(page)
    assert not left, \
        "%d harbor node(s) still drawn after eye-off of %d: %r" % (
            len(left), len(drawn), sorted(left))
    others_after = _cy_len(page, "pc:lighthouse") + _cy_len(page, "p:lighthouse")
    assert others_after == others_before, (
        "eye-off on harbor changed the other projects: %d -> %d cards"
        % (others_before, others_after))
    page.reload()
    probes.wait_layout_idle(page)
    left = _harbor_nodes(page)
    assert not left, "harbor reappeared after reload: %r" % sorted(left)
    pressed = page.evaluate(
        "() => { const r = [...document.querySelectorAll('#project-tree"
        " .pt-project-row')].find(x => x.textContent.includes('harbor'));"
        " return r ? r.parentElement.querySelector('.pt-eye')"
        ".getAttribute('aria-pressed') : null; }")
    assert pressed == "false", \
        "graph hides harbor but tree eye reads aria-pressed=%r after reload" % pressed

@pytest.mark.parametrize("size", _sizes(), indirect=True)
def test_v_side_5(page, site, size):
    """V-SIDE-5: total = all - cancelled everywhere; tree counts match progress counts."""
    _goto(page, site)
    exp = {}
    for it in _SNAP["items"]:
        d, t = exp.get(it["project"], (0, 0))
        t += it["state"] != "cancelled"; d += it["state"] == "done"
        exp[it["project"]] = (d, t)
    exp = {p: "%d/%d" % v for p, v in exp.items()}
    tree = page.evaluate("""() => { const out = {};
      document.querySelectorAll('#project-tree .pt-project-row').forEach(r =>
        out[r.querySelector('.pt-title').textContent.trim()] =
          r.querySelector('.pt-count').textContent.trim()); return out; }""")
    _nav(page, "progress")
    prg = page.evaluate("""() => { const out = {};
      document.querySelectorAll('#panel-progress .prg-table tbody tr').forEach(r =>
        out[r.children[0].textContent.trim()] = r.children[2].textContent.trim());
      return out; }""")
    assert tree == exp, "tree counts %r, spec rule %r" % (tree, exp)
    assert prg == exp, "progress counts %r, spec rule %r" % (prg, exp)

@pytest.mark.parametrize("size", _sizes({"tablet": "UI-GRAPH-CLICK"}), indirect=True)
def test_v_det_1(page, site, size):
    """V-DET-1: details docks right at >= 1200 px, is a bottom sheet below."""
    w, _h = size
    _goto(page, site)
    _select(page, SEL_KEY)
    m = page.evaluate("""() => { const d = document.getElementById('details');
      const r = d.getBoundingClientRect(), cs = getComputedStyle(d);
      return {pos: cs.position, x: r.x, width: r.width, height: r.height,
              vw: window.innerWidth, vh: window.innerHeight}; }""")
    if w < 1200:
        assert m["pos"] == "fixed" and m["width"] == m["vw"] and \
            m["height"] <= 0.6 * m["vh"], "at %d px expected bottom sheet, got %r" % (w, m)
    else:
        assert m["pos"] in ("sticky", "relative") and m["x"] + m["width"] > m["vw"] - 3 \
            and 200 < m["width"] < 0.5 * m["vw"], "at %d px expected dock, got %r" % (w, m)

@pytest.mark.parametrize("size", _sizes("UI-GRAPH-CLICK"), indirect=True)
def test_v_det_2(page, site, size):
    """V-DET-2: a dependency key link selects the dep AND focuses it in the graph."""
    _goto(page, site)
    _select(page, SEL_KEY)
    _click(page, '#details .keylink[title="%s"]' % DEP_KEY)
    page.wait_for_timeout(250)
    assert _h2(page) == DEP_KEY, "details shows %r after dep link" % _h2(page)
    box = probes.rendered_bbox(page, DEP_KEY)
    assert box is not None, \
        "graph never revealed/focused %s (node absent from canvas)" % DEP_KEY

@pytest.mark.parametrize("size", _sizes("UI-GRAPH-CLICK"), indirect=True)
def test_v_det_3(page, site, size):
    """V-DET-3: details text is readable — no [object Object] / undefined."""
    _goto(page, site)
    for key in (SEL_KEY, "orchard:OR-4", "harbor:HB-30",
                "lighthouse:LH-1", "orchard:OR-GOAL"):
        _select(page, key)
        text = page.evaluate("() => document.getElementById('details').textContent")
        junk = text.lower().count("[object object]") + text.count("undefined")
        assert junk == 0, "details for %s has %d junk tokens; text %r" % (key, junk, text[:120])

@pytest.mark.parametrize("size", _sizes("UI-GRAPH-CLICK"), indirect=True)
def test_v_det_4(page, site, size):
    """V-DET-4: one selection renders the panel exactly once."""
    _goto(page, site)
    _nav(page, "table")
    page.evaluate("""() => { window.__h2 = 0;
      const o = document.createElement.bind(document);
      document.createElement = (t, ...a) => {
        if (String(t).toLowerCase() === 'h2') window.__h2++; return o(t, ...a); }; }""")
    _click_row(page, SEL_KEY)
    n = page.evaluate("() => window.__h2")
    assert n == 1, "panel rebuilt %d times per selection (h2 count)" % n

@pytest.mark.parametrize("size", _sizes("UI-GRAPH-CLICK"), indirect=True)
def test_v_det_5(page, site, size):
    """V-DET-5: closes on Escape/close-button/page-switch; stays on Graph and Table."""
    _goto(page, site)
    _select(page, SEL_KEY)
    assert not _hidden(page)
    page.keyboard.press("Escape")
    assert _hidden(page), "Escape did not close the panel"
    _click_row(page, SEL_KEY)
    _click(page, "#details .details-close")
    assert _hidden(page), "close button did not close the panel"
    _click_row(page, SEL_KEY)
    _nav(page, "table")
    assert not _hidden(page), "Table switch closed the panel (spec: stays)"
    _nav(page, "progress")
    assert _sel(page) is None and _hidden(page), \
        "Progress switch kept selection %r with panel open (spec: closes)" % _sel(page)
