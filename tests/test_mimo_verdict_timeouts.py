"""Hermetic regressions for review budgeting and transport completion."""
import ast
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.spawner import inline_diff
from plangraph.spawner.inline_diff import InlineDiffError, inline_diff_prompt
from plangraph.spawner.opencode_answer import final_answer
from plangraph.spawner.run_watchdog_limits import thresholds


@pytest.fixture(autouse=True)
def watchdog_env(monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', raising=False)
    monkeypatch.delenv('PLANGRAPH_SPAWN_WATCHDOG_INTERVAL', raising=False)


def stream(*events):
    return '\n'.join(json.dumps(dict(type=kind, part=part)) for kind, part in events)


def test_review_card_cap_is_a_hard_wall():
    limits = thresholds({'max_min': 30}, role='review')
    assert limits['max_s'] == 1800
    assert limits['hard_max'] is True


@pytest.mark.parametrize('factory', ['service', 'dispatch'])
@pytest.mark.parametrize('leg_id', ['mimo-v2.6-flash', 'mimo-v2.6-pro'])
@pytest.mark.parametrize('max_min', [None, 30])
@pytest.mark.parametrize('timeout', [None, '', '7'])
def test_mimo_verdict_seat_passes_review_role_to_thresholds(
        monkeypatch, tmp_path, factory, leg_id, max_min, timeout, fake_base_fresh):
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner import run_watchdog
    from plangraph.spawner.runner import spawn_service
    from plangraph.spawner.service import SpawnService
    from registry_route_util import _Runner, _repo

    if timeout is not None:
        monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', timeout)
    repo, base, head = _repo(tmp_path)
    chosen = next(leg for leg in DEFAULT_LEGS if leg.id == leg_id)
    plan = SpawnPlan((chosen,), (), 'operator-order', (chosen,), ())
    card = {'estimate_tokens': 100, 'hard_first_edit': True,
            'diff_ref': {'repo': str(repo), 'base': base, 'head': head}}
    if max_min is not None:
        card['max_min'] = max_min
    expected_max = max_min * 60 if max_min is not None else {
        None: 1500, '': 5400, '7': 7}[timeout]
    threshold_calls = []

    def observed_thresholds(card=None, *, role=None):
        threshold_calls.append((card, role))
        return thresholds(card, role=role)

    class CappedRunner(_Runner):
        async def spawn(self, **kwargs):
            job = await super().spawn(**kwargs)
            limits = kwargs['watchdog']
            assert kwargs['role'] == 'review'
            assert limits['max_s'] == expected_max
            assert limits['hard_max'] is True
            assert not limits.get('hard_first_edit', False)
            # Even an accidental writer default cannot extend this seat's wall.
            watchdog = run_watchdog.Watchdog(limits, start=0, edit='baseline')
            assert watchdog.budget() == expected_max
            assert watchdog.decide(expected_max - 1, 'baseline', expected_max - 1, None) is None
            event = watchdog.decide(expected_max, 'baseline', expected_max, None)
            assert event['rule'] == 'max'
            job.status, job.exit_code, job.events = 'timeout', 124, [event]
            return job

    async def admit_plan(plan, **kwargs):
        return plan

    async def no_probe(*args, **kwargs):
        return None

    monkeypatch.setattr(run_watchdog, 'thresholds', observed_thresholds)
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', admit_plan)
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', no_probe)
    runner = CappedRunner()
    runner.default_cwd = str(repo)
    if factory == 'service':
        service = SpawnService(runner=runner, state=tmp_path / 'state')
    else:
        sink = SimpleNamespace(deliver=lambda *args: None)
        service = spawn_service(runner=runner, state=tmp_path / 'state',
                                completion_sink=sink, keeper_sink=sink)
    monkeypatch.setattr(service, '_route', lambda *args, **kwargs: plan)
    result = asyncio.run(service.spawn(card=card, prompt='Review this change', role='review',
                                      writer_families=('claude',), seats=True, wait_seconds=None))
    assert threshold_calls == [(card, 'review')]
    assert len(runner.calls) == 1
    assert runner.calls[0]['model_or_combo'] == chosen.model
    assert '+two' in runner.calls[0]['prompt']
    seat, = result['seats']
    assert seat['role'] == 'review'
    assert seat['status'] == 'timed_out'
    assert seat['watchdog']['rule'] == 'max'


def test_inline_budget_excludes_instructions_and_framing(monkeypatch):
    monkeypatch.setattr(inline_diff, '_check_diff_ref', lambda ref: None)
    monkeypatch.setattr(inline_diff.subprocess, 'run',
                        lambda *a, **kw: SimpleNamespace(stdout='+change\n', stderr='', returncode=0))
    expected = 'x' * 100 + inline_diff.HEADER.format(base='a', head='b') + '+change\n' + inline_diff.FOOTER
    packaged = inline_diff_prompt('x' * 100, {'repo': '.', 'base': 'a', 'head': 'b'},
                                  max_chars=110, timeout=3)
    assert packaged == expected
    assert len(packaged) > 110


def test_explicit_truncation_cannot_be_blessed_by_unfinished_tail():
    events = [dict(type='text', part=dict(text='partial')),
              dict(type='step_finish', part=dict(reason='length')),
              dict(type='text', part=dict(text='VERDICT: PASS'))]
    assert final_answer('\n'.join(map(json.dumps, events)), allow_unfinished=True) is None


@pytest.mark.parametrize('output,code,error', [
    ('', 0, 'empty diff'),
    ('', 128, 'git diff failed'),
    ('x' * 200, 0, 'too large'),
])
def test_packaging_failures_never_truncate_or_fabricate(monkeypatch, output, code, error):
    monkeypatch.setattr(inline_diff, '_check_diff_ref', lambda ref: None)
    monkeypatch.setattr(inline_diff.subprocess, 'run',
                        lambda *a, **kw: SimpleNamespace(stdout=output, stderr='bad revision', returncode=code))
    with pytest.raises(InlineDiffError, match=error):
        inline_diff_prompt('review', {'repo': '.', 'base': 'a', 'head': 'b'}, max_chars=150, timeout=3)


def test_missing_diff_is_not_a_document_review():
    with pytest.raises(InlineDiffError, match='no diff_ref'):
        inline_diff_prompt('review', None, max_chars=1000, timeout=3)


@pytest.mark.parametrize('finish', [
    [('step_finish', {'reason': 'stop'})],
    [('text', {'text': ' \n'}), ('step_finish', {'reason': 'stop'})],
    [('step_finish', {'reason': 'tool-calls'})],
    [('text', {'text': 'tool note'}), ('step_finish', {'reason': 'tool-calls'})],
    [('step_finish', {'reason': 'stop'}), ('step_finish', {'reason': 'tool-calls'})],
])
def test_truncation_survives_finishes_without_a_completed_answer(finish):
    output = stream(('text', {'text': 'partial'}),
                    ('step_finish', {'reason': 'length'}), *finish,
                    ('text', {'text': 'VERDICT: PASS'}))
    assert final_answer(output, allow_unfinished=True) is None


@pytest.mark.parametrize('blank', [' ', ' \n', '\t\r\n'])
@pytest.mark.parametrize('tail', [False, True])
def test_blank_pending_stop_cannot_clear_truncation(blank, tail):
    # Simulate blank text reaching pending if the earlier ingestion filter changes.
    # Completion must enforce its own non-blank invariant, not trust that filter.
    tree = ast.parse(inspect.getsource(final_answer))
    filters = [node for node in ast.walk(tree) if isinstance(node, ast.If)
               and isinstance(node.test, ast.BoolOp)
               and ast.unparse(node.test) == 'isinstance(text, str) and text.strip()']
    assert len(filters) == 1
    filters[0].test = filters[0].test.values[0]
    namespace = {'json': json}
    exec(compile(ast.fix_missing_locations(tree), '<blank-pending>', 'exec'), namespace)
    decode = namespace['final_answer']
    output = stream(('text', {'text': 'partial'}),
                    ('step_finish', {'reason': 'length'}),
                    ('text', {'text': blank}), ('step_finish', {'reason': 'stop'}))
    if tail:
        output += '\n' + stream(('text', {'text': 'VERDICT: PASS'}))
    assert decode(output, allow_unfinished=True) is None
    assert decode(output) is None


@pytest.mark.parametrize('prefix', [[], [
    ('text', {'text': 'tool note'}), ('step_finish', {'reason': 'tool-calls'}),
]])
def test_nontruncated_unfinished_tail_is_eligible_only_when_opted_in(prefix):
    output = stream(*prefix, ('text', {'text': 'VERDICT: PASS'}))
    assert final_answer(output, allow_unfinished=True) == 'VERDICT: PASS'
    assert final_answer(output) is None


@pytest.mark.parametrize('tail', [False, True])
def test_text_bearing_stop_recovers_from_truncation(tail):
    output = stream(('text', {'text': 'partial'}),
                    ('step_finish', {'reason': 'length'}),
                    ('text', {'text': 'complete answer'}),
                    ('step_finish', {'reason': 'stop'}))
    if tail:
        output += '\n' + stream(('text', {'text': 'VERDICT: PASS'}))
    assert final_answer(output, allow_unfinished=True) == (
        'VERDICT: PASS' if tail else 'complete answer')
    assert final_answer(output) == (None if tail else 'complete answer')


@pytest.mark.parametrize('part', [{}, {'reason': None}, {'reason': 'unknown'},
                                  {'reason': 'length'}, {'reason': 'error'}])
def test_unrecognized_finish_blocks_unfinished_tail(part):
    output = stream(('text', {'text': 'VERDICT: PASS'}), ('step_finish', part),
                    ('text', {'text': 'VERDICT: PASS'}))
    assert final_answer(output, allow_unfinished=True) is None


def test_missing_and_unknown_finish_policy_is_documented():
    docs = (Path(__file__).parents[1] / 'docs/SPAWNER.md').read_text()
    section = docs.split('### MiMo seat timeouts', 1)[1].split('\n### ', 1)[0]
    assert 'missing or unknown' in section
    assert 'fail closed' in section
    assert 'whitespace-only' in section


@pytest.mark.parametrize('timeout,max_s', [(None, 1500), ('', 5400), ('7', 7)])
def test_review_without_card_cap_always_has_hard_wall(monkeypatch, timeout, max_s):
    from plangraph.spawner.run_watchdog import Watchdog
    if timeout is not None:
        monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', timeout)
    limits = thresholds(role='review')
    assert limits['max_s'] == max_s
    assert limits['hard_max'] is True
    # Even a consumer's default writer flag cannot extend a review wall.
    watchdog = Watchdog(limits, start=0, edit='baseline')
    assert watchdog.budget() == max_s
    assert watchdog.decide(max_s, 'baseline', max_s, None)['rule'] == 'max'


@pytest.mark.parametrize('max_min', [None, 30])
@pytest.mark.parametrize('timeout', [None, '', '7'])
def test_review_ignores_writer_first_edit_opt_in(monkeypatch, max_min, timeout):
    if timeout is not None:
        monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', timeout)
    card = {'hard_first_edit': True}
    if max_min is not None:
        card['max_min'] = max_min
    before = dict(card)
    limits = thresholds(card, role='review')
    assert not limits.get('hard_first_edit', False)
    assert limits['hard_max'] is True
    assert card == before
    assert thresholds(card, role='implement')['hard_first_edit'] is True


@pytest.mark.parametrize('value', [None, 0, -1, '30', True, float('nan'), float('inf')])
@pytest.mark.parametrize('role', ['review', 'implement'])
def test_invalid_explicit_cap_is_refused(value, role):
    from plangraph.planner.routing import SpawnRefused
    with pytest.raises(SpawnRefused, match='invalid watchdog threshold: max_min'):
        thresholds({'max_min': value}, role=role)


@pytest.mark.parametrize('extra', [-1, 0, 1])
def test_inline_diff_boundary_preserves_main_contract(monkeypatch, extra):
    monkeypatch.setattr(inline_diff, '_check_diff_ref', lambda ref: None)
    monkeypatch.setattr(inline_diff.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(stdout='+change\n', stderr='', returncode=0))
    expected = 'review' + inline_diff.HEADER.format(base='a', head='b') + '+change\n' + inline_diff.FOOTER
    max_chars = len('+change\n') + extra
    if extra < 0:
        with pytest.raises(InlineDiffError) as exc:
            inline_diff_prompt('review', {'repo': '.', 'base': 'a', 'head': 'b'},
                               max_chars=max_chars, timeout=3)
        assert str(exc.value) == 'prompt_too_large: diff too large for leg: 8 > 7 chars'
        assert (exc.value.chars, exc.value.max_chars) == (8, 7)
        return
    packaged = inline_diff_prompt('review', {'repo': '.', 'base': 'a', 'head': 'b'},
                                  max_chars=max_chars, timeout=3)
    assert packaged == expected
    assert len(packaged) > max_chars


def test_packaging_tests_pass_explicit_timeout_and_patch_existing_targets():
    assert callable(inline_diff._check_diff_ref)
    assert callable(inline_diff.subprocess.run)
    tree = ast.parse(Path(__file__).read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'inline_diff_prompt']
    assert calls
    assert all('timeout' in {kw.arg for kw in call.keywords} for call in calls)
