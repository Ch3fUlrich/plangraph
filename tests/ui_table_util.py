"""Shared helpers for the GU-TB1 table model tests (TB1 continuation).

BIG snapshot generator (seeded, tmp-only — never written into tests/fixtures),
the node runner invocation, and the JS bootstrap shared by every TB1 runner.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ui" / "synthetic_snapshot.json"

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

# Bootstrap: load order per task — page, model, store, search, table_model, table_url.
RUNNER_BOOT = r"""
"use strict";
var fs = require("fs");
var mode = process.argv[2];
function emit(o) { process.stdout.write(JSON.stringify(o)); }
var P = JSON.parse(process.argv[3]);
var IN = P.payload ? JSON.parse(fs.readFileSync(P.payload, "utf8")) : null;
require(P.page); var PlanUI = require(P.model);
require(P.store); require(P.search);
require(P.tm); require(P.tu);
function vmOf(p) { return PlanUI.buildViewModel(JSON.parse(fs.readFileSync(p, "utf8"))); }
function deepFreeze(o) {
  if (o && typeof o === "object" && !Object.isFrozen(o)) {
    Object.freeze(o);
    for (var k in o) { if (Object.prototype.hasOwnProperty.call(o, k)) deepFreeze(o[k]); }
  }
  return o;
}
function freezeVm(vm) {
  vm.items.forEach(function (r) { deepFreeze(r); });
  (vm.projects || []).forEach(function (p) { deepFreeze(p); });
  Object.freeze(vm);
  return vm;
}
"""

JS_PATHS = {
    "page": str(WEB / "page.js"),
    "model": str(WEB / "page_model.js"),
    "store": str(WEB / "page_store.js"),
    "search": str(WEB / "page_search.js"),
    "tm": str(WEB / "page_table_model.js"),
    "tu": str(WEB / "page_table_url.js"),
    "fix": str(FIXTURE),
}


def run_node(tmp_path: Path, runner_js: str, mode: str, payload=None, timeout=180) -> dict:
    """Run one TB1 runner mode; payload (JSON-serialisable) is passed via file."""
    runner = tmp_path / "runner.js"
    runner.write_text(runner_js, encoding="utf-8")
    paths = dict(JS_PATHS)
    if payload is not None:
        pf = tmp_path / "payload.json"
        pf.write_text(json.dumps(payload), encoding="utf-8")
        paths["payload"] = str(pf)
    cmd = ["node", str(runner), mode, json.dumps(paths)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def vm_dump(tmp_path: Path):
    """Records + projects of the fixture vm AND the BIG vm, for the Python oracles.
    Returns (dump, big_path)."""
    big = tmp_path / "big.json"
    big.write_text(json.dumps(big_snapshot(150)), encoding="utf-8")
    res = run_node(tmp_path, _DUMP_RUNNER, "records", {"big": str(big)})
    return res, big


_DUMP_RUNNER = RUNNER_BOOT + r"""
if (mode === "records") {
  var out = { records: [], projects: null, recordsBig: null, projectsBig: null };
  var vmf = vmOf(P.fix);
  vmf.items.forEach(function (r) { out.records.push(r); });
  out.projects = vmf.projects;
  if (IN && IN.big) {
    var vmb = vmOf(IN.big);
    out.recordsBig = [];
    vmb.items.forEach(function (r) { out.recordsBig.push(r); });
    out.projectsBig = vmb.projects;
  }
  emit(out);
}
"""

BIG_PROJECTS = ["atlas", "borealis", "cinder", "dune", "ember", "flint", "gale", "hazel"]
BIG_PHASES = ["S0 Found", "S1 Build", "S2 Integ", "S3 Harden", "S4 Pilot", "S10 Launch"]
_OWNERS = [f"Owner-{i:02d}" for i in range(10)]
_WORDS = "Ingest parse Shard route Cache verify Sign publish rotate Audit".split()
_STATES_HOT = ["open", "open", "open", "claimed", "in_review", "cancelled"]
_STATES_EARLY = ["open", "claimed", "in_review"]
_UNI = ["Ünïcode", "naïve", "Zürich"]


def big_snapshot(per_project=150, seed=7) -> dict:
    """The BIG snapshot: 8 projects x per_project items, field shape of
    make_synthetic.py; ~35 % done, a few cancelled/in_review/claimed, ~6 %
    operator, priority 0-3 with many ties, deps on the previous <= 8 keys of
    the same project plus a few cross-project, titles with mixed case, digits
    and a unicode char."""
    R = random.Random(seed)
    items = []
    for pi, proj in enumerate(BIG_PROJECTS):
        keys = []
        for i in range(per_project):
            ph = BIG_PHASES[min(i * len(BIG_PHASES) // per_project, len(BIG_PHASES) - 1)]
            iid = f"{proj}-{i + 1}"
            key = f"{proj}:{iid}"
            pool = keys[-8:]
            deps = R.sample(pool, min(len(pool), R.choice([0, 1, 1, 2, 3]))) if pool else []
            if keys and R.random() < 0.03:
                other = R.choice([p for p in BIG_PROJECTS if p != proj])
                deps.append(f"{other}:{other}-1")
            frac = i / per_project
            r = R.random()
            if r < 0.35:
                st = "done"
            elif frac > 0.5:
                st = R.choice(_STATES_HOT)
            else:
                st = R.choice(_STATES_EARLY)
            title = f"{R.choice(_WORDS)} {R.choice(_WORDS)} {R.choice(_WORDS)} v{i % 7} step {i}"
            if i % 23 == 5:
                title = f"{R.choice(_UNI)} {title}"
            op = R.random() < 0.06
            it = {
                "project": proj, "id": iid, "key": key, "title": title,
                "kind": "gate" if i % 12 == 11 else "lane", "track": ph, "phase": ph,
                "owner": R.choice(_OWNERS), "state": st,
                "estimate_h": R.choice([1, 2, 3, 4, 6, 8]), "depends_on": deps,
                "corrects": None,
                "done_criteria": "Evidence exists and a reviewer confirmed it.",
                "evidence_kinds": ["commit+ci"], "operator": op,
                "priority": R.choice([0, 0, 0, 1, 2, 3]), "epoch": 1, "evidence": [],
                "updated_at": "2026-10-01T00:00:00Z", "claimed_by": None,
                "lease_until": None, "worker_run": None, "reviewer_run": None,
                "review_tier": "standard",
            }
            if st == "done":
                it["evidence"] = [{"kind": "commit+ci", "ref": f"{R.randrange(16**7):07x}",
                                   "verified": True, "at": "2026-10-01T00:00:00Z"}]
            items.append(it)
            keys.append(key)
    return {"items": items, "questions": [], "decisions": []}
