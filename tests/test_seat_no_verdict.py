"""Hermetic LE-SEAT-NO-VERDICT regressions; no harness or gateway."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.route_policy import RoutePolicy
from plangraph.review.verdict import judge
from plangraph.spawner.exit_outcome import terminal_reason
from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.mark.parametrize('text', ['', 'Looks good.', 'In prose VERDICT: pass',
                                     '```\nVERDICT: pass\n```', '> VERDICT: pass',
                                     'VERDICT: pass', 'VERDICT: fix'])
@pytest.mark.parametrize('with_job', [True, False])
def test_review_outcome_uses_judge(text, with_job):
    record = dict(role='review', status='completed', exit_code=0,
                  stdout_tail='VERDICT: pass', attempts=[dict(stdout_tail=text)])
    job = SimpleNamespace(stdout=text, response_finish_reason='stop')
    terminal_reason(record, job if with_job else None)
    missing = judge(text, 'stop').verdict is None
    assert record['status'] == ('failed' if missing else 'completed')
    assert record.get('reason') == ('no_verdict' if missing else None)


@pytest.mark.parametrize('reason', ['cancelled', 'git_config_mutated',
                                   'writer_no_changes', 'kill_incomplete'])
def test_terminal_owner_preserved(reason):
    record = dict(role='review', status='failed', exit_code=0, reason=reason)
    before = record.copy()
    terminal_reason(record, SimpleNamespace(stdout=''))
    assert record == before


def test_writer_unchanged():
    record = dict(role='implement', status='completed', exit_code=0)
    before = record.copy()
    terminal_reason(record, SimpleNamespace(stdout=''))
    assert record == before


def test_catalog_flag(monkeypatch, capsys):
    from plangraph.spawner import cli
    monkeypatch.setattr('plangraph.routing_support.resolver.get_catalog_models',
                        lambda: [dict(id='catalog-alias', model='mimo/mimo-v2.6-flash')])
    assert cli.main(['catalog']) == 0
    assert json.loads(capsys.readouterr().out)[0]['review_unfit'] is True


def test_forced_leg_warning(tmp_path):
    from plangraph.spawner.service import SpawnService
    service = SpawnService(runner=SimpleNamespace(), state=tmp_path)
    record = asyncio.run(service.spawn(
        card={'estimate_tokens': 100}, prompt='Review', role='review',
        leg='mimo-v2.6-flash', writer_families=['gpt'], dry_run=True,
        override_reason='hermetic regression'))
    assert 'review_unfit: mimo/mimo-v2.6-flash' in record['warnings']


@pytest.mark.parametrize('harness,mode,inline', [('qodercli', 'tools', False),
                                               ('opencode', 'tools', False),
                                               ('opencode', 'inline-only', True)])
def test_diff_delivery(monkeypatch, harness, mode, inline):
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=None)
    leg = replace(DEFAULT_LEGS[0], harness=harness, tool_mode=mode)
    card = {'diff_ref': {'repo': 'unused', 'base': 'base', 'head': 'head'}}
    calls = []
    monkeypatch.setattr('plangraph.spawner.context_pack.pack_prompt', lambda p, *a: p)
    def diff(prompt, ref, **kwargs):
        calls.append(ref)
        return prompt + '\nDIFF'
    monkeypatch.setattr('plangraph.spawner.inline_diff.inline_diff_prompt', diff)
    assert seat._leg_prompt('Review', card, 'review', leg) == 'Review' + ('\nDIFF' if inline else '')
    assert calls == ([card['diff_ref']] if inline else [])


def test_existing_policy_and_inline_refusal():
    from plangraph.spawner.inline_diff import InlineDiffError
    leg = replace(DEFAULT_LEGS[0], model='opencode-go/qwen3.8-flash')
    assert RoutePolicy().skip_reason(leg, 'review') == 'no_verdict_seat'
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=None)
    with pytest.raises(InlineDiffError, match='no diff_ref'):
        seat._leg_prompt('Review', {}, 'review', replace(leg, tool_mode='inline-only'))
