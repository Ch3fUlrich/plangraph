"""Tests for PlanUI viewport layout, word wrap, and panZoom with insets (T3b-1fix5)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


@need_node
def test_wrap_label(tmp_path: Path):
    """wrapLabel breaks only at spaces, handles overlong words, and respects maxPx."""
    runner = tmp_path / "wrap_label_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

var measurer = function (s) { return (typeof s === "string" ? s.length : 0) * 10; };

var res1 = PlanUI.wrapLabel("P0 Foundations ▾ 13 tasks · 13 done", 200, measurer);
var res2 = PlanUI.wrapLabel("Supercalifragilisticexpialidocious short", 200, measurer);
var res3 = PlanUI.wrapLabel("The quick brown fox jumps over the lazy dog", 150, measurer);
var res4 = PlanUI.wrapLabel("", 200, measurer);
var res5 = PlanUI.wrapLabel(null, 200, measurer);
var res6 = PlanUI.wrapLabel(12345, 200, measurer);
var res7 = PlanUI.wrapLabel("NoSpacesHere", 200, measurer);
var res8 = PlanUI.wrapLabel("P0 Foundations · 13 tasks · 13 done", 200, measurer);

// T6: exact-fit and invalid measurer termination
var exactFit = PlanUI.wrapLabel("Exact fit", 90, measurer); // length 9 * 10 = 90 == limit
var zeroMeasure = PlanUI.wrapLabel("one two three", 50, function () { return 0; });
var nanMeasure = PlanUI.wrapLabel("one two three", 50, function () { return NaN; });
var infMeasure = PlanUI.wrapLabel("one two three", 50, function () { return Infinity; });

emit({
  res1: res1,
  res2: res2,
  res3: res3,
  res4: res4,
  res5: res5,
  res6: res6,
  res7: res7,
  res8: res8,
  exactFit: exactFit,
  zeroMeasure: zeroMeasure,
  nanMeasure: nanMeasure,
  infMeasure: infMeasure
});
"""
    runner.write_text(script, encoding="utf-8")
    cmd = ["node", str(runner), str(WEB / "page.js"), str(WEB / "page_graph_viewport.js")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    # 1. "P0 Foundations ▾ 13 tasks · 13 done" with maxPx 200
    lines1 = out["res1"].split("\n")
    orig_words1 = ["P0", "Foundations", "▾", "13", "tasks", "13", "done"]
    joined_words1 = [w for line in lines1 for w in line.split(" ") if w]
    assert joined_words1 == orig_words1
    for line in lines1:
        assert len(line) * 10 <= 200

    # lines.join(" ") equals original with " · " replaced by space / words kept
    assert " ".join(lines1) in ("P0 Foundations ▾ 13 tasks 13 done", "P0 Foundations ▾ 13 tasks · 13 done")

    # res8: "P0 Foundations · 13 tasks · 13 done" where second " · " is kept
    lines8 = out["res8"].split("\n")
    for line in lines8:
        assert len(line) * 10 <= 200
    assert "·" in " ".join(lines8)

    # 2. A single word longer than maxPx stays whole on its own line
    lines2 = out["res2"].split("\n")
    assert lines2[0] == "Supercalifragilisticexpialidocious"
    assert len(lines2[0]) * 10 > 200
    assert lines2[1] == "short"
    assert len(lines2[1]) * 10 <= 200

    # 3. Text without " · " is wrapped at spaces only
    lines3 = out["res3"].split("\n")
    assert len(lines3) > 1
    for line in lines3:
        assert len(line) * 10 <= 150
    assert " ".join(lines3) == "The quick brown fox jumps over the lazy dog"

    # 4. Empty and non-string unchanged
    assert out["res4"] == ""
    assert out["res5"] is None
    assert out["res6"] == 12345
    assert out["res7"] == "NoSpacesHere"

    # T6: exact-fit stays on 1 line (kills <= vs < mutant)
    assert out["exactFit"] == "Exact fit"
    assert "\n" not in out["exactFit"]

    # T6: zero, NaN, Infinity measures terminate
    assert out["zeroMeasure"] == "one two three"
    assert out["nanMeasure"] == "one\ntwo\nthree"
    assert out["infMeasure"] == "one\ntwo\nthree"


@need_node
def test_pan_zoom_for_insets(tmp_path: Path):
    """panZoomFor supports inset parameter shifting the visible center."""
    runner = tmp_path / "pan_zoom_inset_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

var casesNoInset = [
  { box: { x: 100, y: 200, w: 300, h: 400 }, size: { w: 1000, h: 800 }, z: 0.8 },
  { box: { x: 50, y: 80, w: 200, h: 100 }, size: { w: 1440, h: 900 }, z: 0.9 },
  { box: { x: 372, y: 150, w: 250, h: 120 }, size: { w: 390, h: 676 }, z: 0.8 }
];

var noInsetResults = casesNoInset.map(function (c) {
  var rOld = PlanUI.panZoomFor(c.box, c.size, c.z);
  var rNew = PlanUI.panZoomFor(c.box, c.size, c.z, null);
  var rEmpty = PlanUI.panZoomFor(c.box, c.size, c.z, {});
  return { c: c, rOld: rOld, rNew: rNew, rEmpty: rEmpty };
});

var phoneBox = { x: 100, y: 300, w: 240, h: 64 };
var phoneSize = { w: 390, h: 676 };
var phoneZ = 0.8;
var phoneInset = { bottom: 120 };
var phoneResult = PlanUI.panZoomFor(phoneBox, phoneSize, phoneZ, phoneInset);

var nonNumericResult = PlanUI.panZoomFor(phoneBox, phoneSize, phoneZ, {
  bottom: "bad", top: null, left: undefined, right: NaN
});

var fourSidedInset = { top: 40, bottom: 60, left: 30, right: 50 };
var fourSidedResult = PlanUI.panZoomFor(phoneBox, phoneSize, phoneZ, fourSidedInset);

var baseNoInset = PlanUI.panZoomFor(phoneBox, phoneSize, phoneZ, null);
var invalidInsets = [
  { top: NaN }, { top: Infinity }, { top: -Infinity }, { top: -20 }, { top: "bad" },
  { bottom: NaN }, { bottom: Infinity }, { bottom: -Infinity }, { bottom: -20 }, { bottom: "bad" },
  { left: NaN }, { left: Infinity }, { left: -Infinity }, { left: -20 }, { left: "bad" },
  { right: NaN }, { right: Infinity }, { right: -Infinity }, { right: -20 }, { right: "bad" }
];
var invalidResults = invalidInsets.map(function (ins) {
  return PlanUI.panZoomFor(phoneBox, phoneSize, phoneZ, ins);
});

emit({
  noInsetResults: noInsetResults,
  phoneResult: phoneResult,
  nonNumericResult: nonNumericResult,
  fourSidedResult: fourSidedResult,
  baseNoInset: baseNoInset,
  invalidResults: invalidResults
});
"""
    runner.write_text(script, encoding="utf-8")
    cmd = ["node", str(runner), str(WEB / "page.js"), str(WEB / "page_graph_viewport.js")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    # 1. No inset equals old result across >= 3 cases
    for item in out["noInsetResults"]:
        c = item["c"]
        r_old = item["rOld"]
        r_new = item["rNew"]
        r_empty = item["rEmpty"]
        assert r_new == r_old
        assert r_empty == r_old

        cx = c["box"]["x"] + c["box"]["w"] / 2.0
        cy = c["box"]["y"] + c["box"]["h"] / 2.0
        expected_pan_x = c["size"]["w"] / 2.0 - cx * c["z"]
        expected_pan_y = c["size"]["h"] / 2.0 - cy * c["z"]
        assert abs(r_old["pan"]["x"] - expected_pan_x) < 1e-6
        assert abs(r_old["pan"]["y"] - expected_pan_y) < 1e-6

    # 2. Phone inset {bottom: 120}, size {w: 390, h: 676}, z 0.8, box {x: 100, y: 300, w: 240, h: 64}
    pr = out["phoneResult"]
    expected_pan_y = (676 - 120) / 2.0 - (300 + 32) * 0.8
    assert abs(pr["pan"]["y"] - expected_pan_y) < 1e-6

    rendered_bottom = pr["pan"]["y"] + (300 + 64) * 0.8
    assert rendered_bottom <= (676 - 120)

    # 3. Non-numeric inset values count as 0
    nn = out["nonNumericResult"]
    old_base = out["noInsetResults"][2]["rOld"]
    expected_default_y = 676 / 2.0 - (300 + 32) * 0.8
    assert abs(nn["pan"]["y"] - expected_default_y) < 1e-6

    # 4. Left/right/top insets shift the centre as stated
    fs = out["fourSidedResult"]
    cx = 100 + 120
    cy = 300 + 32
    expected_fs_x = (390 - 30 - 50) / 2.0 + 30 - cx * 0.8
    expected_fs_y = (676 - 40 - 60) / 2.0 + 40 - cy * 0.8
    assert abs(fs["pan"]["x"] - expected_fs_x) < 1e-6
    assert abs(fs["pan"]["y"] - expected_fs_y) < 1e-6

    # T2: NaN, Infinity, -Infinity, negative and string insets for each side behave like 0
    base_res = out["baseNoInset"]
    for r in out["invalidResults"]:
        assert r["pan"] == base_res["pan"]
        assert r["zoom"] == base_res["zoom"]

    # T2: valid bottom inset centres box in uncovered area (exact number)
    assert abs(out["phoneResult"]["pan"]["x"] - 19.0) < 1e-6
    assert abs(out["phoneResult"]["pan"]["y"] - 12.4) < 1e-6


@need_node
def test_sheet_inset_real_markup(tmp_path: Path):
    """sheetInset with real markup: bottom sheet class is always present, hidden attribute indicates state."""
    runner = tmp_path / "sheet_inset_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

function makeEl(tag, id, cls, hidden, rect) {
  var classes = (cls || "").split(/\s+/).filter(Boolean);
  return {
    tagName: tag.toUpperCase(),
    id: id,
    className: cls || "",
    hidden: !!hidden,
    getAttribute: function (k) { return k === "hidden" ? (hidden ? "" : null) : null; },
    hasAttribute: function (k) { return k === "hidden" ? !!hidden : false; },
    classList: {
      contains: function (c) { return classes.indexOf(c) >= 0; }
    },
    getBoundingClientRect: function () {
      return rect || { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 };
    }
  };
}

function makeDoc(opts) {
  var width = opts.width || 1280;
  var detailsHidden = opts.detailsHidden !== undefined ? opts.detailsHidden : false;
  var sheetTop = opts.sheetTop !== undefined ? opts.sheetTop : 400;
  var sheetHeight = opts.sheetHeight !== undefined ? opts.sheetHeight : (opts.rectHeightZero ? 0 : 300);
  var canvasBottom = opts.canvasBottom !== undefined ? opts.canvasBottom : 800;

  var win = {
    innerWidth: width,
    matchMedia: function (q) {
      if (opts.matchMediaMatch !== undefined) return { matches: opts.matchMediaMatch };
      return { matches: width <= 700 };
    }
  };

  var detEl = opts.noDetails ? null : makeEl(
    "aside",
    "details",
    "details bottom-sheet",
    detailsHidden,
    { top: sheetTop, bottom: sheetTop + sheetHeight, left: 0, right: width, width: width, height: sheetHeight }
  );

  var cyEl = opts.noCy ? null : makeEl(
    "div",
    "cy",
    "cy",
    false,
    { top: 0, bottom: canvasBottom, left: 0, right: width, width: width, height: canvasBottom }
  );

  var doc = {
    defaultView: win,
    getElementById: function (id) {
      if (id === "details") return detEl;
      if (id === "cy") return cyEl;
      return null;
    }
  };
  return { doc: doc, win: win };
}

// 1. Desktop 1280 wide, details hidden => all zero
var d1 = makeDoc({ width: 1280, detailsHidden: true, sheetTop: 400 });
global.window = d1.win;
var r_desktop_hidden = PlanUI.sheetInset(d1.doc, d1.win);

// 2. Desktop 1280 wide, details visible, class bottom-sheet present, rect top 400 => all zero (regression)
var d2 = makeDoc({ width: 1280, detailsHidden: false, sheetTop: 400 });
global.window = d2.win;
var r_desktop_visible = PlanUI.sheetInset(d2.doc, d2.win);

// 3. Phone 390, hidden => 0 (step 1 implementation)
var d3 = makeDoc({ width: 390, detailsHidden: true, sheetTop: 724, canvasBottom: 844 });
global.window = d3.win;
var r_phone_hidden = PlanUI.sheetInset(d3.doc, d3.win);

// 4. Phone, visible, sheet rect top 724 canvas bottom 844 => bottom 120
var d4 = makeDoc({ width: 390, detailsHidden: false, sheetTop: 724, canvasBottom: 844, sheetHeight: 120 });
global.window = d4.win;
var r_phone_visible_120 = PlanUI.sheetInset(d4.doc, d4.win);

// 5. Phone, visible, sheet top 644 => bottom 200
var d5 = makeDoc({ width: 390, detailsHidden: false, sheetTop: 644, canvasBottom: 844, sheetHeight: 200 });
global.window = d5.win;
var r_phone_visible_200 = PlanUI.sheetInset(d5.doc, d5.win);

// 6. Phone, visible, rect height 0 => zero
var d6 = makeDoc({ width: 390, detailsHidden: false, rectHeightZero: true });
global.window = d6.win;
var r_phone_zero_height = PlanUI.sheetInset(d6.doc, d6.win);

// 7. Missing #cy / #details / document => zero
var d7a = makeDoc({ width: 390, noCy: true });
var d7b = makeDoc({ width: 390, noDetails: true });
var r_missing_cy = PlanUI.sheetInset(d7a.doc, d7a.win);
var r_missing_det = PlanUI.sheetInset(d7b.doc, d7b.win);
var r_missing_doc = PlanUI.sheetInset(null);

// 8. Media query honoured: matchMedia stub true with innerWidth 1280 => phone, false with innerWidth 390 => not phone
var d8a = makeDoc({ width: 1280, matchMediaMatch: true, detailsHidden: false, sheetTop: 724, canvasBottom: 844 });
var d8b = makeDoc({ width: 390, matchMediaMatch: false, detailsHidden: false, sheetTop: 724, canvasBottom: 844 });
var r_mq_true_1280 = PlanUI.sheetInset(d8a.doc, d8a.win);
var r_mq_false_390 = PlanUI.sheetInset(d8b.doc, d8b.win);

emit({
  desktop_hidden: r_desktop_hidden,
  desktop_visible: r_desktop_visible,
  phone_hidden: r_phone_hidden,
  phone_visible_120: r_phone_visible_120,
  phone_visible_200: r_phone_visible_200,
  phone_zero_height: r_phone_zero_height,
  missing_cy: r_missing_cy,
  missing_det: r_missing_det,
  missing_doc: r_missing_doc,
  mq_true_1280: r_mq_true_1280,
  mq_false_390: r_mq_false_390
});
"""
    runner.write_text(script, encoding="utf-8")
    cmd = ["node", str(runner), str(WEB / "page.js"), str(WEB / "page_graph_viewport.js")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    assert out["desktop_hidden"] == {"top": 0, "bottom": 0, "left": 0, "right": 0}
    assert out["desktop_visible"] == {"top": 0, "bottom": 0, "left": 0, "right": 0}
    assert out["phone_hidden"] == {"top": 0, "bottom": 0, "left": 0, "right": 0}
    assert out["phone_visible_120"]["bottom"] == 120
    assert out["phone_visible_200"]["bottom"] == 200
    assert out["phone_zero_height"] == {"top": 0, "bottom": 0, "left": 0, "right": 0}
    assert out["missing_cy"] == {"top": 0, "bottom": 0, "left": 0, "right": 0}
    assert out["missing_det"] == {"top": 0, "bottom": 0, "left": 0, "right": 0}
    assert out["missing_doc"] == {"top": 0, "bottom": 0, "left": 0, "right": 0}
    assert out["mq_true_1280"]["bottom"] == 120
    assert out["mq_false_390"] == {"top": 0, "bottom": 0, "left": 0, "right": 0}

