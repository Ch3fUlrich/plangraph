import json
import os
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


@pytest.fixture(autouse=True)
def spawnd(monkeypatch):
    """Publication runs the built spawner binary (``agents-view --publish``)."""
    from _spawnd import built_rust_binary
    from plangraph import fleet_snapshot
    binary = built_rust_binary('pg-spawnd', 'release')
    if not binary.is_file():
        binary = built_rust_binary('pg-spawnd')
    assert binary.is_file(), f'build pg-spawnd before running fleet tests: {binary}'
    monkeypatch.setattr(fleet_snapshot, '_projection_binary', lambda: str(binary))
    return binary


@pytest.fixture(autouse=True)
def words_file(tmp_path_factory, monkeypatch):
    """A present, empty private-word policy; tests of the policy overwrite it."""
    path = tmp_path_factory.mktemp('words') / 'scrub-words.txt'
    path.write_text('# no private words\n')
    monkeypatch.setenv('PLANGRAPH_SCRUB_WORDS', str(path))
    return path

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
    assert view['generated_at'] is None  # not the collector's timestamp shape: dropped
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
    view = dict(generated_at='2026-11-21T06:30:00Z', hosts=[dict(
        host='local', readable=True, reason=None, agents=[row]),
        unavailable('remote', 'unreadable')])
    put(tmp_path, '.local/state/plangraph/agents-view.json', view)
    document = render({'items': []}, tmp_path, cdn=True, live_agents=True)
    assert json.dumps(view, separators=(',', ':')) in document


def test_render_unreadable_private_view_is_null(tmp_path, monkeypatch):
    from plangraph.render import render

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    (tmp_path / '.local/state/plangraph/agents-view.json').mkdir(parents=True)  # unreadable as a file
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


def _published_view(document):
    return json.loads(re.search(
        r'"agents_view":(.*?)(?:,"items"|\}\s*</script>)', document, re.S).group(1))


def _store(tmp_path, view):
    put(tmp_path, '.local/state/plangraph/agents-view.json', view)


def _row(**fields):
    return dict(dict(name='worker', model='model', state='idle', item=None, age_s=3,
                     runtime='opencode'), **fields)


def test_render_masks_secret_shapes_in_stored_view(tmp_path, monkeypatch):
    from plangraph.render import render

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    secret = 'synthetic' + 'Credential' + '0123456789'
    _store(tmp_path, dict(generated_at='2026-11-21T06:30:00Z', hosts=[dict(
        host='Bearer ' + secret, readable=True, agents=[
            _row(name='Authorization: Bearer ' + secret, model='password=' + secret)])]))
    document = render({'items': []}, tmp_path, cdn=True, live_agents=True)
    assert secret not in document


def test_render_applies_current_private_words_to_stored_view(tmp_path, monkeypatch, words_file):
    from plangraph.render import render

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    label, longer = 'synth' + 'label', 'synth' + 'label-display'
    _store(tmp_path, dict(generated_at=label, hosts=[dict(host=label, readable=True, agents=[
        _row(name=longer, model='x-' + label)])]))
    words_file.write_text(label + '\n' + longer + '\n' + 're:\\p{Greek}+\n')
    document = render({'items': []}, tmp_path, cdn=True, live_agents=True)
    assert label not in document and '-display' not in document
    view = _published_view(document)
    assert view['generated_at'] is None
    assert view['hosts'][0]['agents'][0]['name'] == '[redacted]'


@pytest.mark.parametrize('policy', ['missing', 'bad_regex'])
def test_render_suppresses_stored_view_without_usable_private_words(
        tmp_path, monkeypatch, words_file, policy):
    from plangraph.render import render

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    label = 'synth' + 'label'
    _store(tmp_path, dict(generated_at=label, hosts=[dict(host=label, readable=True, agents=[
        _row()])]))
    if policy == 'missing':
        words_file.unlink()
    else:
        words_file.write_text('re:[\n')
    document = render({'items': []}, tmp_path, cdn=True, live_agents=True)
    assert label not in document
    view = _published_view(document)
    assert view['generated_at'] is None
    assert view['hosts'][0]['readable'] is False
    assert view['hosts'][0]['reason'] == 'scrub_words_unavailable'
    assert view['hosts'][0]['agents'] == []


def test_render_without_the_spawner_binary_publishes_no_inventory(tmp_path, monkeypatch):
    from plangraph import fleet_snapshot
    from plangraph.render import render

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    _store(tmp_path, dict(hosts=[unavailable('remote', 'timeout')]))
    monkeypatch.setattr(fleet_snapshot, '_projection_binary',
                        lambda: str(tmp_path / 'absent-binary'))
    assert '"agents_view":null' in render({'items': []}, tmp_path, cdn=True, live_agents=True)


def test_scheduled_local_write_keeps_configured_hosts_unknown_through_render(
        tmp_path, monkeypatch, spawnd):
    from plangraph.render import render

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    put(tmp_path, 'overflow.json', dict(hosts={'far-host': dict(ssh='far.invalid')}))
    env = dict(os.environ, HOME=str(tmp_path), PLANGRAPH_OVERFLOW_HOSTS=str(tmp_path / 'overflow.json'),
               PLANGRAPH_STATE=str(tmp_path / 'state'))
    subprocess.run([str(spawnd), 'agents-view', '--write', '--local'], env=env, check=True,
                   stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=30)
    view = _published_view(render({'items': []}, tmp_path, cdn=True, live_agents=True))
    far = [h for h in view['hosts'] if h['host'] == 'far-host']
    assert far and far[0]['readable'] is False and far[0]['reason'] == 'not_collected'
