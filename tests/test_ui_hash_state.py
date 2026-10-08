"""W7 hash state: the URL hash reproduces the view (round-trip safe codec).

Mutation killed: deleting the `cph=` line in hashEncode (collapse lost from
the URL) fails test_roundtrip_each_new_field; restoring it makes it green.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "plangraph" / "web"

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

HARNESS = r"""
"use strict";
var P = require(process.argv[2]);
var mode = process.argv[3];
function emit(o) { process.stdout.write(JSON.stringify(o)); }
function arr(s) { return s ? Array.from(s).sort() : null; }
function S(a) { return new Set(a); }
if (mode === "roundtrip") {
  var cases = {
    collapsedPhases: {collapsedPhases: S(["pA|x", "pB|a,b"])},
    collapsedProjects: {collapsedProjects: S(["pA"])},
    expanded: {expanded: S(["atlas:T-1"])},
    focusProject: {focusProject: "pA"},
    fProjects: {filters: {projects: S(["pA"]), phases: S([]), edgeMode: "all"}},
    fPhases: {filters: {projects: S([]), phases: S(["x"]), edgeMode: "all"}},
    edgeMode: {filters: {projects: S([]), phases: S([]), edgeMode: "critical"}},
    matchMode: {matchMode: "hide"}
  };
  var out = {};
  Object.keys(cases).forEach(function (k) {
    var h = P.hashEncode(cases[k]);
    var d = P.hashDecode(h);
    out[k] = {h: h,
      cp: arr(d.collapsedPhases), cpr: arr(d.collapsedProjects),
      ex: arr(d.expanded), fp: d.focusProject || null,
      fpr: d.filters ? arr(d.filters.projects) : null,
      fph: d.filters ? arr(d.filters.phases) : null,
      em: d.filters ? d.filters.edgeMode : null, mm: d.matchMode || null};
  });
  emit(out);
} else if (mode === "defaults") {
  emit({hash: P.hashEncode(P.freshDefaults()), empty: P.hashEncode({})});
} else if (mode === "garbage") {
  var r = [];
  ["#sel=%ZZ&q=%ZZ&cph=%ZZ&em=nope&mm=nope&==", "#%%%", null, 42,
   "#cph=&cpr=&ex="].forEach(function (g) {
    try { r.push(P.hashDecode(g)); } catch (e) { r.push({threw: true}); }
  });
  emit(r.map(function (d) {
    return {sel: d.sel || null, q: d.query || null, cp: arr(d.collapsedPhases),
      em: d.filters ? d.filters.edgeMode : null, mm: d.matchMode || null,
      threw: !!d.threw, keys: Object.keys(d).sort()};
  }));
} else if (mode === "legacy") {
  var d = P.hashDecode("#v=table&sel=atlas%3AT-31&q=hello");
  emit({view: d.view, sel: d.sel, query: d.query});
}
"""


def _run(tmp_path: Path, mode: str):
    h = tmp_path / "hash_harness.js"
    h.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", str(h), str(WEB / "page_store.js"), mode],
                           capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@need_node
def test_roundtrip_each_new_field(tmp_path):
    out = _run(tmp_path, "roundtrip")
    assert out["collapsedPhases"]["cp"] == ["pA|x", "pB|a,b"]
    assert out["collapsedProjects"]["cpr"] == ["pA"]
    assert out["expanded"]["ex"] == ["atlas:T-1"]
    assert out["focusProject"]["fp"] == "pA"
    assert out["fProjects"]["fpr"] == ["pA"]
    assert out["fPhases"]["fph"] == ["x"]
    assert out["edgeMode"]["em"] == "critical"
    assert out["matchMode"]["mm"] == "hide"
    for v in out.values():
        assert v["h"].startswith("#") and " " not in v["h"]


@need_node
def test_defaults_encode_empty(tmp_path):
    out = _run(tmp_path, "defaults")
    assert out == {"hash": "", "empty": ""}


@need_node
def test_garbage_never_throws_and_ignores_bad_fields(tmp_path):
    rows = _run(tmp_path, "garbage")
    assert rows[0] == {"sel": None, "q": None, "cp": None, "em": None,
                       "mm": None, "threw": False, "keys": []}
    for r in rows[1:]:
        assert r["threw"] is False and r["keys"] == []


@need_node
def test_legacy_hash_still_decodes(tmp_path):
    assert _run(tmp_path, "legacy") == {"view": "table", "sel": "atlas:T-31", "query": "hello"}


def test_codec_moved_to_store_and_boot_in_budget():
    boot = (WEB / "page_boot.js").read_text(encoding="utf-8")
    store = (WEB / "page_store.js").read_text(encoding="utf-8")
    assert len(boot.splitlines()) <= 400
    assert "function hashEncode" not in boot and "function hashDecode" not in boot
    assert "PlanUI.hashEncode" in store and "PlanUI.hashDecode" in boot
    for key in ("patch.collapsedPhases", "patch.expanded", "patch.filters",
                "patch.matchMode", "patch.focusProject"):
        assert key in boot, key
