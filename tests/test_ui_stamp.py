"""UI-VERSION-STAMP page side (B + V-SHELL-4) — node-run tests for
PlanUI.Stamp.format (same idiom as tests/test_ui_pure.py: plain ``node``,
skipped when node is missing) plus one render() check that the assembled
page carries the stamp code and CSS.

Synthetic data only: no hosts, IPs or home paths in the page or here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"

import sys

sys.path.insert(0, str(SRC))

from plangraph import render as render_mod  # noqa: E402

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

GEN = "2026-10-01T00:00:00Z"
GEN_MS = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
DOT = "\u00b7"  # ·

HARNESS = r"""
"use strict";
var fs = require("fs");
var PlanUI = require(process.argv[2]);           // page_stamp.js -> module.exports
var cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
var out = cases.map(function (c) {
  return PlanUI.Stamp.format(c.sha, c.generated, c.headSeq, c.headHash, c.nowMs);
});
process.stdout.write(JSON.stringify(out));
"""


def run_format(tmp_path: Path, cases: list) -> list:
    harness = tmp_path / "stamp_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    cfile = tmp_path / "cases.json"
    cfile.write_text(json.dumps(cases), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(WEB / "page_stamp.js"), str(cfile)],
        capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def at_age(age_min: int) -> int:
    return GEN_MS + age_min * 60_000


@need_node
def test_format_text_for_the_three_sha_values(tmp_path):
    cases = [
        {"sha": "abc1234567def", "generated": GEN, "headSeq": 42, "headHash": None,
         "nowMs": at_age(1)},
        {"sha": "", "generated": GEN, "headSeq": 42, "headHash": None, "nowMs": at_age(1)},
        {"sha": None, "generated": GEN, "headSeq": None, "headHash": None, "nowMs": at_age(1)},
    ]
    [real, empty, missing] = run_format(tmp_path, cases)
    assert real["text"] == f"code abc1234567def {DOT} data {GEN} {DOT} seq 42"
    assert empty["text"] == f"code unknown {DOT} data {GEN} {DOT} seq 42"
    # missing head_seq shows "-"
    assert missing["text"] == f"code unknown {DOT} data {GEN} {DOT} seq -"


@need_node
def test_title_carries_keeper_head_hash(tmp_path):
    cases = [
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": "deadbeefcafebabe0123456789",
         "nowMs": at_age(1)},
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": "short",
         "nowMs": at_age(1)},
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": None, "nowMs": at_age(1)},
    ]
    [full, short, none] = run_format(tmp_path, cases)
    assert full["title"] == "keeper head deadbeefca"
    assert short["title"] == "keeper head short"
    assert none["title"] == ""  # no hash -> no tooltip text


@need_node
def test_stale_clock_boundary_and_age_text(tmp_path):
    cases = [
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": None, "nowMs": at_age(14)},
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": None, "nowMs": at_age(16)},
        {"sha": "s", "generated": GEN, "headSeq": 1, "headHash": None, "nowMs": at_age(90)},
    ]
    young, aged, hours = run_format(tmp_path, cases)
    assert young["stale"] is False and f"{DOT} age" not in young["text"]
    assert young["ageMin"] == 14
    assert aged["stale"] is True
    assert aged["text"] == f"code s {DOT} data {GEN} {DOT} seq 1 {DOT} age 16 min"
    assert hours["stale"] is True
    assert hours["text"].endswith(f"{DOT} age 1h 30m")


@need_node
def test_missing_or_bad_generated_is_never_stale(tmp_path):
    cases = [
        {"sha": "s", "generated": None, "headSeq": 1, "headHash": None, "nowMs": at_age(999)},
        {"sha": "s", "generated": "nonsense", "headSeq": 1, "headHash": None, "nowMs": at_age(999)},
    ]
    missing, bad = run_format(tmp_path, cases)
    for out in (missing, bad):
        assert out["stale"] is False
        assert out["ageMin"] is None
        assert "age" not in out["text"]
    assert missing["text"] == f"code s {DOT} data - {DOT} seq 1"


SNAP = {"generated": GEN, "head": "abc1234567def0123", "head_seq": 42, "items": []}


@pytest.mark.skipif(
    "page_stamp.js" not in render_mod.JS_FILES,
    reason="page_stamp.js not in JS_FILES yet (the other writer's render.py change "
           "has not landed in this checkout)",
)
def test_rendered_page_carries_stamp_code_and_css():
    html = render_mod.render(SNAP, ROOT / "vendor", cdn=True)
    assert ".stamp-stale" in html          # the CSS rule for V-SHELL-4
    assert "PlanUI.Stamp" in html          # the page_stamp.js code is inlined
