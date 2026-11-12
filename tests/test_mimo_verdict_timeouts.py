"""Hermetic regressions for review budgeting and transport completion."""
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner.inline_diff import InlineDiffError, inline_diff_prompt
from plangraph.spawner.opencode_answer import final_answer
from plangraph.spawner.run_watchdog_limits import thresholds


def test_review_card_cap_is_a_hard_wall(monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', raising=False)
    limits = thresholds({'max_min': 30}, role='review')
    assert limits['max_s'] == 1800
    assert limits['hard_max'] is True


def test_inline_budget_includes_instructions_and_framing(monkeypatch):
    monkeypatch.setattr('plangraph.spawner.inline_diff._check_diff_ref', lambda ref: None)
    monkeypatch.setattr('plangraph.spawner.inline_diff.subprocess.run',
                        lambda *a, **kw: SimpleNamespace(stdout='+change\n', stderr='', returncode=0))
    with pytest.raises(InlineDiffError, match='too large'):
        inline_diff_prompt('x' * 100, {'repo': '.', 'base': 'a', 'head': 'b'}, max_chars=110)


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
    monkeypatch.setattr('plangraph.spawner.inline_diff._check_diff_ref', lambda ref: None)
    monkeypatch.setattr('plangraph.spawner.inline_diff.subprocess.run',
                        lambda *a, **kw: SimpleNamespace(stdout=output, stderr='bad revision', returncode=code))
    with pytest.raises(InlineDiffError, match=error):
        inline_diff_prompt('review', {'repo': '.', 'base': 'a', 'head': 'b'}, max_chars=150)


def test_missing_diff_is_not_a_document_review():
    with pytest.raises(InlineDiffError, match='no diff_ref'):
        inline_diff_prompt('review', None, max_chars=1000)
