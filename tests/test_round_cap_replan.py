"""The round cap starts the replan job: fixtures only (fake diff, findings, job runner; no network)."""
import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.replan_job import (ReplanError, ReplanIO, SLICE_MAX_LINES, close_pr, git_diff)
from plangraph.spawner.round_cap import RoundCapRefused
from plangraph.spawner.service import SpawnService

ITEM = 'plangraph:ROUND-CAP-X'
FINDINGS = ['HIGH a.py:1 - first finding, verbatim', dict(text='MED b.py:2 - second finding'), 'LOW c.py:3 - third']
DIFF = 'diff --git a/a.py b/a.py\n+fake_change = 1\n'


def _seed(state, n=3, pr='7'):
    for i in range(n):
        d = state / 'runs' / f'run-seed{i}'
        d.mkdir(parents=True)
        (d / 'record.json').write_text(json.dumps(dict(
            run_id=d.name, role='implement', item_key=ITEM, round_ref=pr, status='completed', started=i)))


class Fake:
    """Injected ReplanIO: records every call in order."""
    def __init__(self, reply=None, define_error=None):
        self.calls, self.prompts, self.lines = [], [], []
        self.reply = reply if reply is not None else json.dumps(dict(slices=[
            dict(title='first', scope='fix a', files=['a.py'], lines=120, findings=[1, 3]),
            dict(title='second', scope='fix b', files=['b.py'], lines=80, findings=[2])]))
        self.define_error = define_error

    def io(self):
        async def job(card, prompt, plan):
            self.calls.append(('job', card['role'], plan.first_leg.id))
            self.prompts.append(prompt)
            return dict(status='completed', run_id='run-job1', stdout_tail='analysis...\n' + self.reply)

        def define(items):
            self.calls.append(('define', items))
            if self.define_error:
                raise self.define_error

        return ReplanIO(
            item=lambda key: dict(key=key, title='the item', description='ITEM-DESCRIPTION'),
            diff=lambda card, ref: DIFF, job=job, define=define,
            close_pr=lambda card, ref, comment: self.calls.append(('close', ref, comment)),
            notify=lambda item, run_id, line: self.lines.append(line))


def _service(tmp_path, fake, route_error=None):
    svc = SpawnService(object(), state=tmp_path)
    svc.replan_io = fake.io()
    leg = SimpleNamespace(id='planning-leg')

    def route(card, role, *args, **kwargs):
        if route_error:
            raise route_error
        assert role == 'research'
        return SimpleNamespace(first_leg=leg, legs=[leg], error=None)
    svc._route = route
    return svc


def _cap(svc):
    """A 4th implement spawn: refused, then the replan task runs to its end."""
    card = dict(item_key=ITEM, pr='7', findings=FINDINGS)

    async def go():
        with pytest.raises(RoundCapRefused) as err:
            await svc._spawn(card=card, prompt='x')
        payload = err.value.payload
        run_id = payload['replan'].get('run_id')
        if payload['replan']['started']:
            await svc.tasks[run_id]
        return payload
    return asyncio.run(go())


def test_cap_runs_replan_that_defines_slices_closes_pr_and_tells_routing(tmp_path):
    _seed(tmp_path)
    fake = Fake()
    payload = _cap(_service(tmp_path, fake))
    assert payload['replan']['status'] == 'running' and payload['replan_leg'] == 'planning-leg'
    assert [c[0] for c in fake.calls] == ['job', 'define', 'close']  # define first: a failed define keeps the PR
    assert fake.calls[0][1:] == ('research', 'planning-leg')
    prompt = fake.prompts[0]
    assert DIFF in prompt and 'ITEM-DESCRIPTION' in prompt
    assert all(t in prompt for t in ('HIGH a.py:1 - first finding, verbatim', 'MED b.py:2 - second finding', 'LOW c.py:3 - third'))
    items = fake.calls[1][1]
    assert [i['id'] for i in items] == ['ROUND-CAP-X-S1', 'ROUND-CAP-X-S2']
    assert all(i['project'] == 'plangraph' and i['under'] == ITEM and len(i['done_criteria']) <= 600 for i in items)
    assert 'HIGH a.py:1 - first finding, verbatim' in items[0]['description'] and 'LOW c.py:3 - third' in items[0]['description']
    assert 'MED b.py:2 - second finding' in items[1]['description']
    assert fake.calls[2][1] == '7' and 'plangraph:ROUND-CAP-X-S1' in fake.calls[2][2]
    assert len(fake.lines) == 1 and fake.lines[0].startswith(f'DONE {ITEM} 7 ') and '\n' not in fake.lines[0]
    marker = next((tmp_path / 'replan').glob('*[!s].json'))
    assert json.loads(marker.read_text())['status'] == 'done'


@pytest.mark.parametrize('reply,reason', [
    (json.dumps(dict(slices=[dict(title='t', scope='s', lines=SLICE_MAX_LINES + 1, findings=[1, 2, 3])])), 'slice_too_large'),
    (json.dumps(dict(slices=[dict(title='t', scope='s', lines=10, findings=[1, 2])])), 'findings_unassigned: 3'),
    ('no json here', 'slices_missing')])
def test_bad_slices_define_nothing_and_keep_the_pr(tmp_path, reply, reason):
    _seed(tmp_path)
    fake = Fake(reply=reply)
    _cap(_service(tmp_path, fake))
    assert [c[0] for c in fake.calls] == ['job']
    assert len(fake.lines) == 1 and fake.lines[0].startswith(f'FAIL {ITEM} ') and reason in fake.lines[0]


def test_define_refused_keeps_pr_open_and_saves_definitions(tmp_path):
    _seed(tmp_path)
    fake = Fake(define_error=ReplanError('keeper call failed: RefusedCli: forbidden'))
    _cap(_service(tmp_path, fake))
    assert [c[0] for c in fake.calls] == ['job', 'define']
    saved = json.loads(next((tmp_path / 'replan').glob('*.items.json')).read_text())
    assert len(saved) == 2 and 'define_failed' in fake.lines[0] and '.items.json' in fake.lines[0]


def test_second_refusal_starts_no_second_replan(tmp_path):
    _seed(tmp_path)
    fake = Fake()
    svc = _service(tmp_path, fake)
    _cap(svc)
    again = _cap(svc)
    assert again['replan']['status'] == 'done' and again['replan']['started'] is False
    assert [c[0] for c in fake.calls].count('job') == 1 and len(fake.lines) == 1


def test_no_planning_leg_starts_no_job_and_blocks_to_routing(tmp_path):
    _seed(tmp_path)
    fake = Fake()
    payload = _cap(_service(tmp_path, fake, route_error=SpawnRefused('no planning leg up')))
    assert payload['replan']['status'] == 'blocked' and payload['replan_route_error'] == 'no planning leg up'
    assert fake.calls == [] and len(fake.lines) == 1 and fake.lines[0].startswith(f'BLOCKED {ITEM} ')


def test_default_commands(tmp_path):
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='DIFF', stderr='')
    assert git_diff(str(tmp_path), dict(pr='7'), '7', run) == 'DIFF'
    assert git_diff(str(tmp_path), dict(diff_ref=dict(repo='/r', base='b', head='h')), None, run) == 'DIFF'
    close_pr(str(tmp_path), '7', 'why', run)
    assert seen == [['gh', 'pr', 'diff', '7'], ['git', '-C', '/r', 'diff', '--no-ext-diff', '--no-color', 'b...h', '--'],
                    ['gh', 'pr', 'close', '7', '--comment', 'why']]
    with pytest.raises(ReplanError, match='gh failed'):
        close_pr(str(tmp_path), '7', 'why', lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout='', stderr='boom\n'))
