"""Hermetic regressions for review budgeting and transport completion."""
import ast
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


def test_inline_budget_includes_instructions_and_framing(monkeypatch):
    monkeypatch.setattr(inline_diff, '_check_diff_ref', lambda ref: None)
    monkeypatch.setattr(inline_diff.subprocess, 'run',
                        lambda *a, **kw: SimpleNamespace(stdout='+change\n', stderr='', returncode=0))
    with pytest.raises(InlineDiffError, match='too large'):
        inline_diff_prompt('x' * 100, {'repo': '.', 'base': 'a', 'head': 'b'}, max_chars=110, timeout=3)


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


def test_spawner_document_stays_within_file_size_limit():
    docs = Path(__file__).parents[1] / 'docs/SPAWNER.md'
    assert len(docs.read_text().splitlines()) <= 400


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


def test_exact_fit_inline_package_is_accepted(monkeypatch):
    monkeypatch.setattr(inline_diff, '_check_diff_ref', lambda ref: None)
    monkeypatch.setattr(inline_diff.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(stdout='+change\n', stderr='', returncode=0))
    expected = 'review' + inline_diff.HEADER.format(base='a', head='b') + '+change\n' + inline_diff.FOOTER
    max_chars = len(expected)
    packaged = inline_diff_prompt('review', {'repo': '.', 'base': 'a', 'head': 'b'},
                                  max_chars=max_chars, timeout=3)
    assert packaged == expected
    assert len(packaged) == max_chars


def test_packaging_tests_pass_explicit_timeout_and_patch_existing_targets():
    assert callable(inline_diff._check_diff_ref)
    assert callable(inline_diff.subprocess.run)
    tree = ast.parse(Path(__file__).read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'inline_diff_prompt']
    assert calls
    assert all('timeout' in {kw.arg for kw in call.keywords} for call in calls)
