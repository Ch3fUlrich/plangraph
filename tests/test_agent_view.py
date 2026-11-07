import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import pytest

def put(root, path, value):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value))
    return target

@pytest.fixture
def home(tmp_path):
    return tmp_path

def unavailable(host, reason):
    return dict(host=host, readable=False, reason=reason, agents=[])

def test_render_reads_private_file(home, monkeypatch):
    from plangraph.render import render
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    put(home, '.local/state/plangraph/agents-view.json', {'generated_at': 'stamp', 'hosts': [unavailable('remote', 'timeout')]})
    assert 'remote' in render({'items': []}, home, cdn=True, live_agents=True)
    document = render({'items': [], 'agents_view': {'hosts': [unavailable('supplied', 'timeout')]}}, home, cdn=True, live_agents=True)
    assert 'supplied' not in document
    assert 'remote' in document
    assert '"agents_view":' in document
    assert '"readable":false' in document.replace(' ', '')


def test_page_unknown(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    node_binary = shutil.which('node')
    if node_binary is None:
        pytest.skip('Node.js is required for the page behavior test')
    monkeypatch.chdir(tmp_path)
    script = r'''
const assert = require('assert');
function node(tag) { return {tag, children: [], appendChild(n) {this.children.push(n);},
  removeChild() {this.children.shift();}, get firstChild() {return this.children[0];}}; }
const box = node('div');
global.document = {createElement: node, getElementById: () => box};
global.PlanPage = {state: {snapshot: {agents_view: {generated_at: '2000-01-01',
  hosts: [{host: 'remote', readable: false, agents: []}]}}}};
global.PlanAgentsSchema = require(process.argv[2]);
require(process.argv[1]);
PlanUI.AgentsPage.render();
const row = box.children[1].children[1].children[0];
assert.equal(row.children[0].textContent, 'remote');
assert(row.children.slice(1).every(n => n.textContent === 'unknown'));
assert(box.children[0].textContent.includes('stale'));
assert.equal(box.children[1].children[0].children[6].textContent, 'age');
global.PlanPage.state.snapshot.agents_view.generated_at = 'invalid';
PlanUI.AgentsPage.render();
assert(box.children[0].textContent.includes('unknown'));
global.PlanPage.state.snapshot.agents_view.hosts = [{readable: true, agents: null}];
PlanUI.AgentsPage.render();
assert.equal(box.children[1].children[1].children.length, 1);
global.PlanPage.state.snapshot = {};
PlanUI.AgentsPage.render();
assert.equal(box.children[1].children[1].children.length, 1);
'''
    subprocess.run([node_binary, '-e', script, str(root / 'src/plangraph/web/page_agents.js'), str(root / 'src/plangraph/web/agents_schema.json')], check=True)
    assert '@@AGENTS_UI@@' in (root / 'src/plangraph/web/page_template.html').read_text()
    assert 'agents: "panel-agents"' in (root / 'src/plangraph/web/page_pages.js').read_text()


@pytest.mark.parametrize('data', [None, [], 'invalid', 1, {}, {'hosts': None}])
def test_render_malformed_view_is_unknown(home, data):
    from plangraph.render import render
    document = render({'items': [], 'agents_view': data}, home, cdn=True, live_agents=True)
    assert '"agents_view":null' in document


def test_snapshot_producer_excludes_private_view(home, monkeypatch):
    from keeper_util import World
    from plangraph.keeper.snapshot_io import write_snapshot
    from plangraph.render import render

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    put(home, '.local/state/plangraph/agents-view.json', {'hosts': [
        {'host': 'local', 'readable': True, 'agents': [
            {'name': 'live-agent', 'runtime': 'opencode', 'credential': 'excluded'}]},
        unavailable('remote', 'timeout')]})
    world = World(home / 'keeper', backend='sqlite')
    try:
        snapshot = world.store.snapshot(world.p('reader'))
        server = SimpleNamespace(store=world.store, system=world.p('reader'),
                                 config=SimpleNamespace(snapshot=home / 'snapshot.json'))
        write_snapshot(server, server.system)
        written = json.loads((home / 'snapshot.json').read_text())
        assert 'agents_view' not in snapshot
        assert 'agents_view' not in written
        document = render(written, home, cdn=True, live_agents=True)
        assert 'live-agent' in document and 'excluded' not in document
        assert '"agents_view":' in document
    finally:
        world.close()

@pytest.mark.parametrize('cdn', [False, True])
def test_nonlive_never_reads_inventory(home, monkeypatch, cdn):
    from plangraph.render import render
    def forbidden(cls):
        raise AssertionError('private inventory accessed')
    monkeypatch.setattr(Path, 'home', classmethod(forbidden))
    document = render({'items': [], 'agents_view': {'hosts': []}}, home, cdn=cdn)
    assert '"agents_view":' not in document
    assert 'id="tab-agents"' not in document
    assert 'id="panel-agents"' not in document
    assert 'UI.AgentsPage =' not in document


def test_live_offline_bundles_agents(home):
    from plangraph.render import render
    document = render({'items': []}, home, live_agents=True)
    assert 'id="tab-agents"' in document
    assert 'UI.AgentsPage =' in document
