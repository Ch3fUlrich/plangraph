import json
import re
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


def test_render_masks_private_view_strings(tmp_path, monkeypatch):
    from plangraph.keeper.credential_guard import find_shapes
    from plangraph.render import render

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    raw = 'login ' + 'operator' + '/' + 'syntheticpass'
    assert find_shapes(raw)
    row = dict(name=raw, model=raw, state=raw, item=raw, age_s=raw,
               runtime='opencode')
    put(tmp_path, '.local/state/plangraph/agents-view.json', dict(
        generated_at=raw, hosts=[dict(host=raw, readable=True, agents=[row])]))
    document = render({'items': []}, tmp_path, cdn=True, live_agents=True)
    assert raw not in document
    assert ('operator' + '/' + 'syntheticpass') not in document
    view = json.loads(re.search(
        r'<script[^>]*id="snapshot-data"[^>]*>(.*?)</script>', document, re.S).group(1))['agents_view']
    assert view['generated_at'] == 'login [redacted]'
    host = view['hosts'][0]
    assert host['host'] == 'login [redacted]'
    assert host['reason'] is None
    assert host['agents'][0] == dict(
        host='login [redacted]', runtime='opencode',
        **{field: 'login [redacted]' for field in row if field != 'runtime'})


def test_render_clean_private_view_unchanged(tmp_path, monkeypatch):
    from plangraph.render import render

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    row = dict(host='local', name='worker', runtime='opencode', model='model',
               state='idle', item=None, age_s=3)
    view = dict(generated_at='stamp', hosts=[dict(
        host='local', readable=True, reason=None, agents=[row]),
        unavailable('remote', 'unreadable')])
    put(tmp_path, '.local/state/plangraph/agents-view.json', view)
    document = render({'items': []}, tmp_path, cdn=True, live_agents=True)
    assert json.dumps(view, separators=(',', ':')) in document


def test_render_unreadable_private_view_is_null(tmp_path, monkeypatch):
    from plangraph.render import render

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    target = put(tmp_path, '.local/state/plangraph/agents-view.json', {'hosts': []})
    read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == target:
            raise PermissionError('private inventory unreadable')
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', unreadable)
    document = render({'items': []}, tmp_path, cdn=True, live_agents=True)
    assert '"agents_view":null' in document


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
  hosts: [{host: 'remote', readable: false, reason: 'source_unreadable', agents: []}]}}}};
global.PlanAgentsSchema = require(process.argv[2]);
require(process.argv[1]);
PlanUI.AgentsPage.render();
const row = box.children[1].children[1].children[0];
assert.equal(row.children[0].textContent, 'remote');
assert(row.children.slice(1).every(n => n.textContent === 'unknown'));
assert.equal(row.title, 'source_unreadable');
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
def test_render_malformed_view_is_unknown(home, data, monkeypatch):
    from plangraph.render import render
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
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


def test_live_offline_bundles_agents(home, monkeypatch):
    from plangraph.render import render
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    document = render({'items': []}, home, live_agents=True)
    assert 'id="tab-agents"' in document
    assert 'UI.AgentsPage =' in document
    assert '"agents_view":null' in document


@pytest.mark.parametrize('private', [None, '{'])
def test_live_missing_private_never_uses_snapshot(home, monkeypatch, private):
    from plangraph.render import render
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    if private is not None:
        target = home / '.local/state/plangraph/agents-view.json'
        target.parent.mkdir(parents=True)
        target.write_text(private)
    document = render({'items': [], 'agents_view': {'hosts': [unavailable('unvetted', 'timeout')]}}, home, live_agents=True)
    assert 'unvetted' not in document
    assert '"agents_view":null' in document
