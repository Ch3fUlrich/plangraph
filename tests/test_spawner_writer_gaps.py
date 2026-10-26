import asyncio

import pytest

from plangraph.spawner.fixer import split_findings, render_fixer_brief, remaining_gaps


@pytest.mark.parametrize('count,sizes', [(0, []), (10, [10]), (20, [10, 10]), (21, [7, 7, 7])])
def test_balanced(count, sizes):
    findings = [{'text': str(i), 'files': [f'f{i}']} for i in range(count)]
    groups = split_findings(findings)
    assert sorted(map(len, groups)) == sizes
    assert sorted(f['text'] for g in groups for f in g) == sorted(f['text'] for f in findings)


def test_transitive_files_and_unassigned():
    findings = [{'text': str(i), 'files': [f'f{i}']} for i in range(20)]
    findings += [{'text': 'bridge', 'files': ['f0', 'f1']}, {'text': 'unknown'}]
    groups = split_findings(findings)
    owners = {}
    for index, group in enumerate(groups):
        for finding in group:
            for path in finding.get('files', []):
                assert owners.setdefault(path, index) == index
    assigned = split_findings(findings[:-1])
    smallest = min(range(len(assigned)), key=lambda i: len(assigned[i]))
    assert groups[smallest][-1]['text'] == 'unknown'


def test_brief():
    brief = render_fixer_brief([{'text': 'one'}, {'text': 'two'}])
    assert '1. [ ] one' in brief and '2. [ ] two' in brief
    assert 'The run ends only when each item is fixed or refuted by a named test' in brief
    assert 'COMMIT before finishing' in brief
    assert 'TEST-SPEED-AFFECTED' in brief


@pytest.mark.parametrize('text,expected', [
    ('## Remaining gaps\n- fix alpha\n## Tests\npassed', ['fix alpha']),
    ('REPORT\n- [ ] fix beta\n- [x] done', ['fix beta']),
    ('Remaining gaps: none', []), ('## Remaining gaps\nNone.\n', []),
    ('Remaining gaps: fix gamma', ['fix gamma']),
])
def test_parse(text, expected):
    assert remaining_gaps(text) == expected


def test_continuation(tmp_path):
    from plangraph.spawner.service import SpawnService
    class Runner:
        async def spawn(self, **kwargs):
            self.calls.append(kwargs)
            return type('Job', (), dict(job_id='child', stdout='Remaining gaps: still broken',
                exit_code=0, status='completed', served_model='test', served_family='test'))()
        calls = []
        _tasks = {}
    async def run():
        runner = Runner()
        service = SpawnService(runner=runner, state=tmp_path)
        record = dict(run_id='run-parent', role='implement', exit_code=0, cwd=str(tmp_path),
                      chosen_leg={'model': 'test', 'harness': 'opencode'})
        await service._continue_writer(record, 'REPORT\nRemaining gaps: fix alpha')
        assert len(runner.calls) == 1
        assert runner.calls[0]['cwd'] == str(tmp_path)
        assert 'fix alpha' in runner.calls[0]['prompt']
        child = service.result(record['continuation_run_id'])
        assert child['parent_run_id'] == record['run_id'] and child['continuation'] == 1
        assert child['continuation_reason'] == 'continuation_limit'
        for changes, output, reason in [({}, 'Remaining gaps: none', 'clean_report'),
                ({'exit_code': 1}, 'Remaining gaps: broken', 'nonzero_exit'),
                ({'continuation': 1}, 'Remaining gaps: broken', 'continuation_limit')]:
            other = dict(record, **changes)
            await service._continue_writer(other, output)
            assert other['continuation_reason'] == reason
        assert len(runner.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize('hardened', [False, True])
def test_dispatch_paths(tmp_path, hardened):
    from plangraph.spawner.runner import JobInfo, spawn_service
    from plangraph.spawner.service import SpawnService
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS

    class Runner:
        default_cwd = str(tmp_path)
        _tasks = {}
        _processes = {}
        provider_responses = {}

        def __init__(self):
            self.calls = []

        async def spawn(self, **kwargs):
            self.calls.append(kwargs)
            return JobInfo(job_id=str(len(self.calls)), status='completed',
                task_prompt=kwargs['prompt'], model_or_combo=kwargs['model_or_combo'],
                exit_code=0, stdout='## Remaining gaps\n- finish one\n', cwd=str(tmp_path))

    async def run():
        runner = Runner()
        service = spawn_service(runner=runner, state=tmp_path) if hardened else SpawnService(runner=runner, state=tmp_path)
        service._route = lambda *a, **kw: SpawnPlan((DEFAULT_LEGS[0],), (), 'test', (), ())
        findings = [{'text': f'finding {i}', 'files': [f'f{i}']} for i in range(20)]
        result = await service.spawn({'estimate_tokens': 1000, 'findings': findings}, wait_seconds=None)
        assert len(result['writers']) == 2
        assert len(runner.calls) == 4
        for parent in result['writers']:
            assert parent['continuation_reason'] == 'remaining_gaps'
            child = service.result(parent['continuation_run_id'])
            assert child['parent_run_id'] == parent['run_id']
            assert child['continuation_reason'] == 'continuation_limit'
        original = [c for c in runner.calls if 'finding' in c['prompt']]
        assert len(original) == 2
        assert all(c['prompt'].count('- [ ] ') == 10 and c['prompt'].count('    finding ') == 10 for c in original)
        for call in runner.calls:
            if 'finding' not in call['prompt']:
                assert 'finish one' in call['prompt']
                assert call['cwd'] == str(tmp_path)
    asyncio.run(run())
