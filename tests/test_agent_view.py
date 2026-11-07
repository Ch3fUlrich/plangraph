import ast
import json
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from plangraph.spawner import agent_view as av


def put(root, path, value):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value))
    return target


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_STATE', raising=False)
    monkeypatch.delenv('XDG_STATE_HOME', raising=False)
    monkeypatch.delenv('PLANGRAPH_OVERFLOW_HOSTS', raising=False)
    return tmp_path


def test_sources_and_projection(home):
    record = dict(name='worker', state='running', model='model', started=10,
                  item='PROJECT:ITEM', credential='do-not-project')
    put(home, '.local/state/plangraph/runs/one/record.json', record)
    put(home, '.local/state/plangraph/sessions/one.json', record)
    put(home, '.claude/jobs/one/state.json', dict(record, createdAt='2026-01-01T00:00:00Z'))
    lane = put(home, 'lane.json', record)
    put(home, '.config/autoos/oc-l1.json', {'lanes': {'one': {'state_file': str(lane)}, 'idle': {}}})
    data = av.local(home, 'local', 20)
    assert data['readable']
    assert [r['runtime'] for r in data['agents']] == ['spawner-run', 'spawner-run', 'claude', 'opencode']
    assert all(set(r) == set(av.FIELDS) and r['age_s'] == 10 for r in data['agents'])
    assert 'do-not-project' not in json.dumps(data)


def test_malformed_and_finished(home):
    put(home, '.claude/jobs/done/state.json', {'state': 'done'})
    path = put(home, '.claude/jobs/bad/state.json', {})
    path.write_text('{')
    data = av.local(home)
    assert data['readable'] is True
    assert len(data['agents']) == 1


@pytest.mark.parametrize('mode,reason', [('timeout', 'timeout'), ('exit', 'ssh_failed'), ('bad', 'invalid_response')])
def test_remote_errors(mode, reason):
    def run(argv, **kwargs):
        assert isinstance(argv, list) and kwargs['timeout'] == 3
        if mode == 'timeout':
            raise subprocess.TimeoutExpired(argv, 3)
        return SimpleNamespace(returncode=int(mode == 'exit'), stdout='bad')
    assert av.remote('remote', {'ssh': 'alias', 'spawner_src': '/source'}, run, 3) == av.unavailable('remote', reason)


def test_no_route():
    assert av.remote('remote', {'ssh': None})['reason'] == 'no_route'


def test_remote_parallel_and_quotes(home):
    put(home, '.config/plangraph/overflow-hosts.json', {'hosts': {
        name: {'ssh': name, 'spawner_src': '/source with space', 'env_file': '/env file'}
        for name in ('slow', 'fast')}})
    barrier = threading.Barrier(2)
    def run(argv, **kwargs):
        assert "'/source with space'" in argv[-1] and "'/env file'" in argv[-1]
        barrier.wait(timeout=2)
        if argv[-2] == 'slow':
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        return SimpleNamespace(returncode=0, stdout=json.dumps({'hosts': [dict(
            readable=True, agents=[{'name': 'one', 'credential': 'do-not-project'}])]}))
    data = av.collect(home, run, timeout=1)
    assert data['hosts'][1]['reason'] == 'timeout'
    assert data['hosts'][2]['readable']
    assert set(data['hosts'][2]['agents'][0]) == set(av.FIELDS)


def test_atomic_write_and_render_projection(home, monkeypatch):
    data = {'generated_at': 'now', 'hosts': [dict(host='local', readable=True,
            agents=[{'name': 'one', 'credential': 'do-not-project'}])]}
    av.write(data, home)
    path = home / '.local/state/plangraph/agents-view.json'
    assert path.stat().st_mode & 0o777 == 0o600
    assert 'do-not-project' not in path.read_text()
    assert list(path.parent.iterdir()) == [path]
    assert 'do-not-project' not in json.dumps(av.load_view(home))
    assert av.load_view(home / 'absent') is None


def assert_no_runtime_invocation(text):
    import re
    tree = ast.parse(text)
    # Scan every literal, including assignments and helper arguments. A standalone
    # runtime label is data; argv-shaped strings/lists are forbidden everywhere.
    assert not re.search(r"(?:/[^\s'\"]*/)?claude\s+(?:ps|status)\b", text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            values = node.elts
            assert not (values and isinstance(values[0], ast.Constant)
                        and isinstance(values[0].value, str)
                        and values[0].value.rsplit('/', 1)[-1] == 'claude'
                        and len(values) > 1 and isinstance(values[1], ast.Constant)
                        and values[1].value in {'ps', 'status'})
        if isinstance(node, ast.Call):
            assert not any(k.arg == 'shell' and isinstance(k.value, ast.Constant)
                           and k.value.value is True for k in node.keywords)


def test_no_runtime_invocation():
    assert_no_runtime_invocation(Path(av.__file__).read_text())
    from plangraph.spawner import agent_view_projection as projection
    assert_no_runtime_invocation(Path(projection.__file__).read_text())


@pytest.mark.parametrize('invocation', [
    "os.system('claude ps')", "subprocess.run('claude ps')",
    "runner(['/bin/claude', 'ps'])", "runner(args='claude ps')",
    "runner(['echo'], shell=True)",
    "cmd = 'claude ps'; runner(cmd)",
    "ingest('claude status')",
])
def test_runtime_guard_rejects_invocations(invocation):
    with pytest.raises(AssertionError):
        assert_no_runtime_invocation(invocation)


def test_projector_scope():
    assert Path(av.project.__code__.co_filename).parent.name == 'spawner'


def test_atomic_failure_preserves_snapshot(home, monkeypatch):
    av.write({'hosts': []}, home)
    import plangraph.routing_support.private_io as private_io
    def fail(*args):
        raise OSError('replace failed')
    monkeypatch.setattr(private_io.os, 'replace', fail)
    with pytest.raises(OSError):
        av.write({'hosts': ['replacement']}, home)
    path = home / '.local/state/plangraph/agents-view.json'
    assert json.loads(path.read_text())['hosts'] == []
    assert list(path.parent.iterdir()) == [path]


def test_render_reads_private_file(home, monkeypatch):
    from plangraph.render import render
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    av.write({'generated_at': 'stamp', 'hosts': [av.unavailable('remote', 'timeout')]}, home)
    assert 'remote' in render({'items': []}, home, cdn=True)
    document = render({'items': [], 'agents_view': {'hosts': [av.unavailable('supplied', 'timeout')]}}, home, cdn=True)
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
require(process.argv[1]);
PlanUI.AgentsPage.render();
const row = box.children[1].children[1].children[0];
assert.equal(row.children[0].textContent, 'remote');
assert(row.children.slice(1).every(n => n.textContent === 'unknown'));
assert(box.children[0].textContent.includes('stale'));
global.PlanPage.state.snapshot.agents_view.hosts = [{readable: true, agents: null}];
PlanUI.AgentsPage.render();
assert.equal(box.children[1].children[1].children.length, 1);
global.PlanPage.state.snapshot = {};
PlanUI.AgentsPage.render();
assert.equal(box.children[1].children[1].children.length, 1);
'''
    subprocess.run([node_binary, '-e', script, str(root / 'src/plangraph/web/page_agents.js')], check=True)
    assert 'tab-agents' in (root / 'src/plangraph/web/page_template.html').read_text()
    assert 'agents: "panel-agents"' in (root / 'src/plangraph/web/page_pages.js').read_text()


@pytest.mark.parametrize('entry', ['x', {'readable': True, 'agents': ['x']}])
def test_remote_non_objects(entry):
    def run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({'hosts': [entry]}))
    assert av.remote('remote', {'ssh': 'alias', 'spawner_src': '/source'}, run)['reason'] == 'invalid_response'


def test_snapshot_non_object_row(home):
    av.write({'hosts': [dict(host='local', readable=True, agents=['x'])]}, home)
    assert av.load_view(home)['hosts'][0]['agents'][0]['name'] is None


def test_bad_source_preserves_valid(home):
    put(home, '.local/state/plangraph/runs/run-one/record.json', {'name': 'live', 'state': 'running'})
    bad = put(home, '.claude/jobs/one/state.json', {})
    bad.write_text('{')
    data = av.local(home, 'local')
    assert data['readable']
    assert [r['name'] for r in data['agents']] == ['live', None]


def test_session_store_sidecars_are_not_agents(home):
    put(home, '.local/state/plangraph/sessions/one.json', {'name': 'one', 'state': 'running'})
    put(home, '.local/state/plangraph/sessions/one.ready.json', {'ready': True})
    put(home, '.local/state/plangraph/sessions/_counters.json', {'one': 1})
    assert [r['name'] for r in av.local(home)['agents']] == ['one']


def test_false_ended_is_running(home):
    put(home, '.local/state/plangraph/runs/one/record.json', {'state': 'running', 'ended': False})
    assert len(av.local(home)['agents']) == 1


def test_bad_config_unavailable(home):
    path = put(home, '.config/autoos/oc-l1.json', {})
    path.write_text('{')
    assert av.local(home)['readable'] is False


def test_mixed_projection(home):
    put(home, '.local/state/plangraph/agents-view.json', {'hosts': [
        {'host': 'good', 'readable': True, 'agents': [{'name': 'live'}]},
        {'host': 'bad', 'readable': True, 'agents': ['x']}, 'x']})
    view = av.load_view(home)
    assert view['hosts'][0]['agents'][0]['name'] == 'live'
    assert view['hosts'][1]['agents'][0]['name'] is None
    assert view['hosts'][2]['readable'] is False


def test_outer_deadline(home):
    import time
    put(home, '.config/plangraph/overflow-hosts.json', {'hosts': {
        name: {'ssh': name, 'spawner_src': '/source'} for name in ('slow', 'fast')}})
    release = threading.Event()
    def run(argv, **kwargs):
        if argv[-2] == 'slow':
            release.wait(2)
        return SimpleNamespace(returncode=0, stdout='{"hosts":[{"readable":true,"agents":[]}]}')
    start = time.monotonic()
    try:
        data = av.collect(home, run, timeout=0.05)
        assert time.monotonic() - start < 0.5
        assert data['hosts'][1]['reason'] == 'timeout'
        assert data['hosts'][2]['readable'] is True
    finally:
        release.set()


def test_outer_deadline_includes_scheduling_slack(home, monkeypatch):
    put(home, '.config/plangraph/overflow-hosts.json', {'hosts': {
        'remote': {'ssh': 'alias', 'spawner_src': '/source'}}})
    original_wait = av.wait
    def wait(futures, timeout):
        assert timeout > 0.05
        return original_wait(futures, timeout=timeout)
    monkeypatch.setattr(av, 'wait', wait)
    def run(*args, **kwargs):
        threading.Event().wait(0.06)  # completion plus scheduler overhead
        return SimpleNamespace(returncode=0, stdout=json.dumps({'hosts': [
            {'readable': True, 'agents': [{'name': 'live'}]}]}))
    assert av.collect(home, run, timeout=0.05)['hosts'][1]['agents'][0]['name'] == 'live'


def test_unreadable_state(home, monkeypatch):
    (home / '.local/state/plangraph').mkdir(parents=True)
    def denied(*args):
        raise PermissionError('denied')
    monkeypatch.setattr(av.os, 'scandir', denied)
    assert av.local(home)['readable'] is False


def test_render_projection_matches_public_schema():
    from plangraph.render import _project_agents_view
    from plangraph.spawner.agent_view_projection import project_view

    data = {'generated_at': 'now', 'secret': 'excluded', 'hosts': [
        None, {'host': 'missing', 'readable': False},
        {'host': 'local', 'readable': True, 'agents': [None, {
            'host': 'spoofed', 'name': 'one', 'runtime': 'invalid',
            'model': True, 'state': 'running', 'item': 'demo:A',
            'age_s': 2, 'secret': 'excluded'}]}]}
    assert _project_agents_view(data) == project_view(data)


@pytest.mark.parametrize('data', [None, [], 'invalid', 1, {}, {'hosts': None}])
def test_render_malformed_view_is_unknown(home, data):
    from plangraph.render import render
    document = render({'items': [], 'agents_view': data}, home, cdn=True)
    assert '"agents_view":null' in document


def test_snapshot_producer_excludes_private_view(home, monkeypatch):
    from keeper_util import World
    from plangraph.keeper.snapshot_io import write_snapshot
    from plangraph.render import render

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    put(home, '.local/state/plangraph/agents-view.json', {'hosts': [
        {'host': 'local', 'readable': True, 'agents': [
            {'name': 'live-agent', 'runtime': 'opencode', 'credential': 'excluded'}]},
        av.unavailable('remote', 'timeout')]})
    world = World(home / 'keeper', backend='sqlite')
    try:
        snapshot = world.store.snapshot(world.p('reader'))
        server = SimpleNamespace(store=world.store, system=world.p('reader'),
                                 config=SimpleNamespace(snapshot=home / 'snapshot.json'))
        write_snapshot(server, server.system)
        written = json.loads((home / 'snapshot.json').read_text())
        assert 'agents_view' not in snapshot
        assert 'agents_view' not in written
        document = render(written, home, cdn=True)
        assert 'live-agent' in document and 'excluded' not in document
        assert '"agents_view":' in document
    finally:
        world.close()
