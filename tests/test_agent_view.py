import json
from pathlib import Path
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
    assert [r['runtime'] for r in data['agents']] == ['spawner-run', 'claude', 'claude', 'opencode']
    assert all(set(r) == set(av.FIELDS) and r['age_s'] == 10 for r in data['agents'])
    assert 'do-not-project' not in json.dumps(data)


def test_malformed_and_finished(home):
    put(home, '.claude/jobs/done/state.json', {'state': 'done'})
    path = put(home, '.claude/jobs/bad/state.json', {})
    path.write_text('{')
    data = av.local(home)
    assert data['readable'] is False
    assert data['reason'] == 'source_unreadable'
    assert not data['agents']


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
    assert json.loads(path.read_text()) == data
    assert list(path.parent.iterdir()) == [path]
    assert 'do-not-project' not in json.dumps(av.load_view(home))
    assert av.load_view(home / 'absent') is None


def test_no_runtime_invocation():
    text = Path(av.__file__).read_text()
    assert "['claude'" not in text and '["claude"' not in text
    assert 'shell=True' not in text


def test_atomic_failure_preserves_snapshot(home, monkeypatch):
    av.write({'hosts': []}, home)
    import plangraph.routing_support.private_io as private_io
    def fail(*args):
        raise OSError('replace failed')
    monkeypatch.setattr(private_io.os, 'replace', fail)
    with pytest.raises(OSError):
        av.write({'hosts': ['replacement']}, home)
    path = home / '.local/state/plangraph/agents-view.json'
    assert json.loads(path.read_text()) == {'hosts': []}
    assert list(path.parent.iterdir()) == [path]


def test_render_reads_private_file(home, monkeypatch):
    from plangraph.render import render
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    av.write({'generated_at': 'stamp', 'hosts': [av.unavailable('remote', 'timeout')]}, home)
    document = render({'items': []}, home, cdn=True)
    assert '"agents_view":' in document
    assert '"readable":false' in document.replace(' ', '')


def test_page_unknown():
    script = r'''
const assert = require('assert');
function node(tag) { return {tag, children: [], appendChild(n) {this.children.push(n);},
  removeChild() {this.children.shift();}, get firstChild() {return this.children[0];}}; }
const box = node('div');
global.document = {createElement: node, getElementById: () => box};
global.PlanPage = {state: {snapshot: {agents_view: {generated_at: '2000-01-01',
  hosts: [{host: 'remote', readable: false, agents: []}]}}}};
require('./src/plangraph/web/page_agents.js');
PlanUI.AgentsPage.render();
const row = box.children[1].children[1].children[0];
assert.equal(row.children[0].textContent, 'remote');
assert(row.children.slice(1).every(n => n.textContent === 'unknown'));
assert(box.children[0].textContent.includes('stale'));
global.PlanPage.state.snapshot = {};
PlanUI.AgentsPage.render();
assert.equal(box.children[1].children[1].children.length, 1);
'''
    subprocess.run(['node', '-e', script], check=True)
    assert 'tab-agents' in Path('src/plangraph/web/page_template.html').read_text()
    assert 'agents: "panel-agents"' in Path('src/plangraph/web/page_pages.js').read_text()
