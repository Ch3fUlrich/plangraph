"""Hermetic snapshot-only host view, with a deliberately HTML-hostile fake DOM."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plangraph.render import JS_FILES, render

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src/plangraph/web"
HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const nodes = {};
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {};
    this.classList = {toggle() {}}; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children.splice(this.children.indexOf(c), 1); }
  get firstChild() { return this.children[0]; }
  set textContent(v) { this.text = String(v); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(c => c.textContent).join('|'); }
  set innerHTML(v) { throw Error('HTML forbidden'); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(k, f) { this.listeners[k] = f; }
}
global.document = {createElement: t => new El(t), getElementById: id => nodes[id],
  body: new El('body'), querySelectorAll: () => []};
nodes['sb-panel-hosts'] = new El('div');
global.PlanPage = {state: {snapshot: JSON.parse(process.argv[3])}};
require(process.argv[2] + '/page_hosts.js');
PlanUI.HostsPage.render();
const box = nodes['sb-panel-hosts'];
const table = box.children.find(c => c.tag === 'table');
const rows = table ? table.children.find(c => c.tag === 'tbody').children.map(
  r => r.children.map(c => c.textContent)) : [];
nodes['panel-hosts'] = new El('section'); nodes['tab-hosts'] = new El('button');
require(process.argv[2] + '/page_pages.js');
PlanUI.Pages.mount(null);
PlanUI.Pages.show('graph');
// Execute the actual boot hash matcher and afterInit block against minimal dependencies.
const boot = fs.readFileSync(process.argv[2] + '/page_boot.js', 'utf8');
const matcher = boot.match(/var HASH_PG = (.*?);/)[0];
const writer = boot.slice(boot.indexOf('  function writeHash()'),
  boot.indexOf('\n  /* ---- keyboard map:'));
const after = boot.slice(boot.indexOf('  function afterInit()'), boot.indexOf('\n  boot();'));
const context = {location: {hash: '#pg=hosts'}, PlanUI, PP: {initData() {}, setTab() {}, state: {}},
  global: {}, ui: {get() {return 'graph';}},
  setScreen() {}, $() {}, resizeGraph() {}};
PlanUI.hashEncode = () => '';
vm.runInNewContext(matcher + writer + after + '\nafterInit();', context);
const deepLinkPage = PlanUI.Pages.current();
PlanUI.Pages.show('graph');
context.location.hash = '';
nodes['tab-hosts'].listeners.click();
process.stdout.write(JSON.stringify({rows, text: box.textContent, hash: context.location.hash, deepLinkPage,
  page: PlanUI.Pages.current(), selected: nodes['tab-hosts'].attrs['aria-selected'],
  hidden: nodes['panel-hosts'].hidden}));
"""


def view(tmp_path, snapshot):
    if not shutil.which("node"):
        pytest.skip("node not available")
    script = tmp_path / "hosts.js"
    script.write_text(HARNESS)
    proc = subprocess.run(["node", str(script), str(WEB), json.dumps(snapshot)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_one_row_per_host_including_unknown(tmp_path):
    out = view(tmp_path, {"fleet": {"hosts": [
        {"host_id": "z", "sample": None}, {"host_id": "a", "class": "worker"},
        None, [], "bad", {"host_id": {}, "sample": []}]}})
    assert len(out["rows"]) == 6
    assert [r[0] for r in out["rows"]] == ["a"] + ["unknown"] * 4 + ["z"]
    assert out["rows"][-1][3:] == ["unknown"] * 7


def test_zero_distinct_from_null(tmp_path):
    sample = dict(cpu_percent=0, ram_available_bytes=None, gpu_percent=0,
                  network_rx_bytes_per_s=0, network_tx_bytes_per_s=None, disk_free_bytes=0)
    out = view(tmp_path, {"fleet": {"hosts": [{"host_id": "a", "sample": sample,
                                              "caps": {"max_writers": 0}, "running_agents": 0}]}})
    assert out["rows"][0][3:] == ["0", "unknown", "0", "0", "unknown", "0", "0"]
    assert "max_writers=0" in out["rows"][0][2]


def test_fleet_count_is_supplied_record_count(tmp_path):
    out = view(tmp_path, {"fleet": {"hosts": [{"running_agents": 2}], "running_agents": 7}})
    assert "Fleet running agents: 7" in out["text"]
    assert out["rows"][0][-1] == "2"


@pytest.mark.parametrize("fleet", [None, {}, {"hosts": None}])
def test_missing_fleet_is_unavailable(tmp_path, fleet):
    assert "unavailable" in view(tmp_path, {"fleet": fleet})["text"]
    known = view(tmp_path, {"fleet": {"hosts": [], "running_agents": 0}})
    assert "unavailable" not in known["text"] and "Fleet running agents: 0" in known["text"]


def test_html_in_host_id_is_text(tmp_path):
    value = '<img src=x onerror="throw 1">'
    assert view(tmp_path, {"fleet": {"hosts": [{"host_id": value}]}})["rows"][0][0] == value


def test_hosts_tab_and_hash_roundtrip(tmp_path):
    out = view(tmp_path, {})
    assert out["deepLinkPage"] == "hosts"
    assert (out["page"], out["hash"], out["selected"], out["hidden"]) == ("hosts", "#pg=hosts", "true", False)
    template = (WEB / "page_template.html").read_text()
    assert 'aria-controls="panel-hosts"' in template
    assert 'role="tabpanel" aria-labelledby="tab-hosts"' in template


def test_render_includes_hosts_asset():
    assert JS_FILES.index("page_hosts.js") < JS_FILES.index("page_boot.js")
    html = render({"items": []}, ROOT / "vendor", cdn=True)
    assert "PlanUI.HostsPage =" in html
    assert 'id="sb-panel-hosts"' in html


def test_persisted_registry_snapshot_hosts_page(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from keeper_util import World
    from plangraph.keeper.snapshot_io import read_snapshot, write_snapshot
    from plangraph.spawner.service import SpawnService

    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(tmp_path / "spawn"))
    world = World(tmp_path, backend="sqlite")
    try:
        world.store.upsert_bus_hosts([
            dict(host=h, node_id=h, key_id=h, endpoint=None,
                 health="offline", last_seen=None) for h in ("z", "a", "idle")])
        service = SpawnService(state=tmp_path / "spawn")
        for host, run_id, status in (("a", "run-live1", "running"),
                                     ("a", "run-live2", "running"),
                                     ("z", "run-done", "completed")):
            monkeypatch.setenv("PLANGRAPH_HOST", host)
            service._save(dict(run_id=run_id, status=status))
        path = tmp_path / "snapshot.json"
        write_snapshot(SimpleNamespace(store=world.store,
                       config=SimpleNamespace(snapshot=path)), world.p("system"))
        out = view(tmp_path, read_snapshot(path))
        assert [r[0] for r in out["rows"]] == ["a", "idle", "z"]
        assert all(r[3:-1] == ["unknown"] * 6 for r in out["rows"])
        assert [r[-1] for r in out["rows"]] == ["2", "0", "0"]
        assert "Fleet running agents: 2" in out["text"]
        assert all(r[3:-1] == ["unknown"] * 6 for r in out["rows"][1:])
        path.write_text('{"items": []}')
        assert "unavailable" in view(tmp_path, read_snapshot(path))["text"]
    finally:
        world.close()
