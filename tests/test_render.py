"""Tests for the self-contained human page generator (WP3).

Written test-first (TDD). The renderer must produce ONE self-contained HTML string
with every vendor script and the snapshot inlined, no network references, and the
page's own JS logic must agree with the SPEC on ready / blocked / critical path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
VENDOR = ROOT / "vendor"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "page_snapshot.json"
PAGE_JS = SRC / "plangraph" / "web" / "page.js"

sys.path.insert(0, str(SRC))

from plangraph import render as render_mod  # noqa: E402

SNAPSHOT = json.loads(FIXTURE.read_text(encoding="utf-8"))


def html_for(snapshot=None) -> str:
    return render_mod.render(SNAPSHOT if snapshot is None else snapshot, VENDOR)


def embedded_snapshot(html: str) -> dict:
    m = re.search(
        r'<script[^>]*id="snapshot-data"[^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    assert m, "snapshot-data script block not found"
    return json.loads(m.group(1))


# --------------------------------------------------------------------------- #
# shape and self-containment
# --------------------------------------------------------------------------- #

def test_render_returns_one_html_string():
    html = html_for()
    assert isinstance(html, str)
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "<html" in html and "</html>" in html
    # exactly one document
    assert html.lower().count("<!doctype html") == 1


def test_self_contained_no_external_references():
    html = html_for()
    # every <script> is inline: none carries a src attribute
    for tag in re.findall(r"<script\b[^>]*>", html, re.IGNORECASE):
        assert "src" not in tag.lower(), tag
    # no stylesheet or image tags at all
    assert not re.search(r"<link\b", html, re.IGNORECASE)
    assert not re.search(r"<img\b", html, re.IGNORECASE)
    # no http(s) reference in any src/href attribute
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', html, re.IGNORECASE)
    # no network-capable URL in code that would fetch (defensive)
    assert "fetch(" not in html.replace("fetching", "")


def test_vendor_scripts_are_inlined():
    html = html_for()
    for name in ("cytoscape.min.js", "dagre.min.js", "cytoscape-dagre.js"):
        body = (VENDOR / name).read_text(encoding="utf-8")
        assert body in html, f"{name} was not inlined"
    # page assets are inlined too
    assert (SRC / "plangraph" / "web" / "page.css").read_text(encoding="utf-8") in html
    assert (SRC / "plangraph" / "web" / "page.js").read_text(encoding="utf-8") in html


def test_contains_every_item_key():
    html = html_for()
    for item in SNAPSHOT["items"]:
        assert item["key"] in html, item["key"]


# --------------------------------------------------------------------------- #
# edge cases
# --------------------------------------------------------------------------- #

def test_render_empty_items_does_not_crash():
    html = render_mod.render({"generated": "2026-10-01T00:00:00Z", "head": None, "items": []}, VENDOR)
    assert isinstance(html, str)
    assert html.lstrip().lower().startswith("<!doctype html")


def test_render_missing_items_key_does_not_crash():
    html = render_mod.render({"generated": "2026-10-01T00:00:00Z", "head": None}, VENDOR)
    assert isinstance(html, str)


def test_json_embedded_safely():
    snap = json.loads(json.dumps(SNAPSHOT))
    payload = "evil</script><script>alert(1)</script>"
    snap["items"][0]["title"] = payload
    html = render_mod.render(snap, VENDOR)
    # the raw title must not terminate the embedded script block
    assert "</script><script>alert(1)" not in html
    # the data still round-trips through the embedded JSON
    assert embedded_snapshot(html)["items"][0]["title"] == payload
    # every opening script has exactly one matching close (nothing injected)
    assert html.lower().count("<script") == html.lower().count("</script>")


def test_cli_writes_file(tmp_path):
    snap_path = tmp_path / "s.json"
    out_path = tmp_path / "plan.html"
    snap_path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run(
        [sys.executable, "-m", "plangraph.render", "--snapshot", str(snap_path), "--out", str(out_path)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert out_path.exists()
    written = out_path.read_text(encoding="utf-8")
    assert written.lstrip().lower().startswith("<!doctype html")
    assert "routing:IMP1" in written


# --------------------------------------------------------------------------- #
# page logic under node (DOM-free), skipped cleanly when node is absent
# --------------------------------------------------------------------------- #

NODE_HARNESS = r"""
"use strict";
var fs = require("fs");
var PG = require(process.argv[2]);
var snap = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
var items = snap.items || [];
var out = {
  states: PG.computeDisplayState(items),
  critical: PG.criticalPath(items),
  emptyCrit: PG.criticalPath([]),
  ready: PG.readyList(items).map(function (i) { return i.key; }),
  blocked: PG.blockedBy(items),
  inProgress: PG.inProgress(items).map(function (i) { return i.key; }),
  pctEstimate: PG.percentDone(items, "estimate"),
  pctCount: PG.percentDone(items, "count"),
  unblocks: PG.unblocksMap(items)
};
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_page_logic_under_node(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(PAGE_JS), str(FIXTURE)],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    states = out["states"]
    # ready = open with all deps done
    assert states["routing:IMP1"] == "ready"
    assert states["autoos:WP2"] == "ready"
    # blocked = open/claimed with an unmet dependency
    assert states["routing:IMP2"] == "blocked"
    assert states["routing:IMP3"] == "blocked"
    assert states["routing:GATE1"] == "blocked"
    # non-computed states pass through
    assert states["autoos:WP3"] == "claimed"
    assert states["routing:IMP4"] == "in_review"
    assert states["routing:IMP5"] == "done"
    assert states["routing:IMP6"] == "cancelled"

    # ready list contains exactly the two ready items
    assert sorted(out["ready"]) == ["autoos:WP2", "routing:IMP1"]
    # blocked-by lists the unmet dependency keys
    assert out["blocked"]["routing:IMP2"] == ["routing:IMP1"]
    assert out["blocked"]["routing:IMP3"] == ["routing:IMP1"]
    # in-progress list covers claimed and in_review
    assert sorted(out["inProgress"]) == ["autoos:WP3", "autoos:WP4", "routing:IMP3", "routing:IMP4"]

    # critical path: longest remaining path by estimate_h, ends at a goal
    crit = out["critical"]["keys"]
    assert crit[-1] == "routing:GOAL"
    assert crit[0] == "routing:IMP1"
    assert out["critical"]["hours"] == pytest.approx(13.0)

    # percent done is a sane fraction and differs by measure
    assert 0.0 < out["pctEstimate"] < 1.0
    assert 0.0 < out["pctCount"] < 1.0
    # IMP1 is waited on by three non-terminal items (IMP2, IMP3, GATE1)
    assert out["unblocks"]["routing:IMP1"] == 3
    # empty graph is a clean empty result, not an error
    assert out["emptyCrit"] == {"keys": [], "hours": 0}


def test_fixture_meets_spec_requirements():
    items = SNAPSHOT["items"]
    assert len(items) >= 12
    assert len({i["project"] for i in items}) >= 2
    assert {i["state"] for i in items} >= {"open", "claimed", "in_review", "done", "cancelled"}
    assert any(i["kind"] == "goal" for i in items)
    assert any(i["state"] == "cancelled" for i in items)
    keys = {i["key"] for i in items}
    assert len(keys) == len(items)  # keys unique
    assert all(i["key"] == i["project"] + ":" + i["id"] for i in items)
    # every dependency resolves
    for it in items:
        for dep in it["depends_on"]:
            assert dep in keys, (it["key"], dep)
    # at least one cross-project edge
    cross = [
        (it["key"], dep)
        for it in items
        for dep in it["depends_on"]
        if not dep.startswith(it["project"] + ":")
    ]
    assert cross
    # corrects points at a done item, and the reverse link exists
    for it in items:
        if it.get("corrects"):
            target = next(x for x in items if x["key"] == it["corrects"])
            assert target["state"] == "done"
            assert it["key"] in target.get("corrected_by", [])
