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


@pytest.fixture(autouse=True)
def seat_identity(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_ITEM', 'demo:REVIEW')


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
    """No-regression guard: terminal owners predate this change."""
    record = dict(role='review', status='failed', exit_code=0, reason=reason)
    before = record.copy()
    terminal_reason(record, SimpleNamespace(stdout=''))
    assert record == before


def test_writer_unchanged():
    """No-regression guard: non-review classification stays byte-identical."""
    record = dict(role='implement', status='completed', exit_code=0)
    before = record.copy()
    terminal_reason(record, SimpleNamespace(stdout=''))
    assert record == before


def test_review_null_exit_requires_verdict():
    record = dict(role='review', status='completed', exit_code=None)
    terminal_reason(record, SimpleNamespace(stdout='Review complete'))
    assert record['status'] == 'failed'
    assert record['reason'] == 'no_verdict'


@pytest.mark.parametrize('text,expected', [('', 'no_verdict'), ('VERDICT: pass', None)])
def test_review_stale_quota_stamp(text, expected):
    record = dict(role='review', status='completed', exit_code=0, reason='harness_limit')
    terminal_reason(record, SimpleNamespace(stdout=text))
    assert record.get('reason') == expected
    assert record['status'] == ('failed' if expected else 'completed')


def test_review_ended_on_quota_leg():
    record = dict(role='review', status='failed', exit_code=1, reason='harness_limit')
    before = record.copy()
    terminal_reason(record, SimpleNamespace(stdout=''))
    assert record == before


@pytest.mark.parametrize('selection', [dict(model='omniroute/cx-gpt-6.1-sol'), dict(leg='cx-gpt-6.1-sol')])
def test_live_implement_initial_chosen_leg_none(tmp_path, monkeypatch, selection):
    from plangraph.spawner.service import SpawnService
    saves = []
    async def launch(**kwargs):
        raise RuntimeError('hermetic launch boundary')
    service = SpawnService(runner=SimpleNamespace(spawn=launch, default_cwd=str(tmp_path)), state=tmp_path)
    monkeypatch.setattr(service, '_leg_prompt', lambda *a, **k: 'Implement')
    monkeypatch.setattr('plangraph.spawner.context_pack.annotate', lambda *a: None)
    save = service._save
    def capture(record):
        saves.append(json.loads(json.dumps(record)))
        save(record)
    monkeypatch.setattr(service, '_save', capture)
    asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='Implement',
        role='implement', override_reason='hermetic regression', wait_seconds=None, **selection))
    assert saves[0]['chosen_leg'] is None


def test_catalog_flag(monkeypatch, capsys):
    from plangraph.spawner import cli
    monkeypatch.setattr('plangraph.routing_support.resolver.get_catalog_models',
                        lambda: [dict(id='catalog-alias', model='mimo/mimo-v2.6-flash')])
    assert cli.main(['catalog']) == 0
    assert json.loads(capsys.readouterr().out)[0]['review_unfit'] is True


@pytest.mark.parametrize('selection', [dict(leg='mimo-v2.6-flash'),
                                       dict(model='mimo/mimo-v2.6-flash')])
def test_forced_leg_warning(tmp_path, monkeypatch, selection):
    from plangraph.spawner.service import SpawnService
    service = SpawnService(runner=SimpleNamespace(), state=tmp_path)
    saved = []
    save = service._save
    def capture(record):
        saved.append(json.loads(json.dumps(record)))
        save(record)
    monkeypatch.setattr(service, '_save', capture)
    record = asyncio.run(service.spawn(
        card={'estimate_tokens': 100}, prompt='Review', role='review',
        **selection, writer_families=['gpt'], dry_run=True,
        override_reason='hermetic regression'))
    assert record['chosen_leg']['model'] == 'mimo/mimo-v2.6-flash'
    assert 'review_unfit: mimo/mimo-v2.6-flash' in record['warnings']
    assert saved[0]['ended'] is None
    assert saved[0]['warnings'] == ['review_unfit: mimo/mimo-v2.6-flash']


def test_classifier_does_not_emit_spawn_warning():
    record = dict(role='review', status='running', plan={'p_source': 'override'},
                  chosen_leg={'model': 'mimo/mimo-v2.6-flash'})
    terminal_reason(record)
    terminal_reason(record)
    assert 'warnings' not in record


def test_routed_warning_absent(tmp_path, monkeypatch):
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.service import SpawnService
    chosen = next(l for l in DEFAULT_LEGS if l.model == 'mimo/mimo-v2.6-flash')
    service = SpawnService(runner=SimpleNamespace(), state=tmp_path)
    monkeypatch.setattr(service, '_route', lambda *a, **k: SpawnPlan((chosen,), (), 'prior', (), ()))
    record = asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='Review',
        role='review', writer_families=['gpt'], dry_run=True))
    assert record['chosen_leg']['model'] == chosen.model
    assert 'warnings' not in record


@pytest.mark.parametrize('explicit', [True, False])
def test_real_spawn_warning_selection(tmp_path, monkeypatch, explicit):
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.service import SpawnService
    chosen = next(l for l in DEFAULT_LEGS if l.model == 'mimo/mimo-v2.6-flash')
    launches, saves = [], []
    async def launch(**kwargs):
        launches.append(kwargs)
        raise RuntimeError('hermetic launch boundary')
    service = SpawnService(runner=SimpleNamespace(spawn=launch, default_cwd=str(tmp_path)), state=tmp_path)
    monkeypatch.setattr(service, '_route', lambda *a, **k: SpawnPlan((chosen,), (), 'prior', (), ()))
    monkeypatch.setattr(service, '_leg_prompt', lambda *a, **k: 'Review')
    save = service._save
    def capture(record):
        saves.append(json.loads(json.dumps(record)))
        save(record)
    monkeypatch.setattr(service, '_save', capture)
    record = asyncio.run(service.spawn(
        card={'estimate_tokens': 100}, prompt='Review', role='review',
        writer_families=['gpt'], override_reason='hermetic regression', wait_seconds=1,
        **({'leg': chosen.id} if explicit else {})))
    assert len(launches) == 1
    expected = ['review_unfit: mimo/mimo-v2.6-flash'] if explicit else []
    assert saves[0].get('warnings', []) == expected
    assert record.get('warnings', []) == expected


@pytest.mark.parametrize('mode', ['tools', 'inline-only'])
@pytest.mark.parametrize('has_diff', [False, True])
def test_routed_spawn_diff_delivery(tmp_path, monkeypatch, mode, has_diff):
    from plangraph.spawner.inline_diff import InlineDiffError
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=str(tmp_path))
    chosen = replace(DEFAULT_LEGS[0], tool_mode=mode)
    card = {'diff_ref': {'repo': str(tmp_path), 'base': 'base', 'head': 'head'}} if has_diff else {}
    monkeypatch.setattr('plangraph.spawner.context_pack.pack_prompt', lambda p, *a: p)
    monkeypatch.setattr('plangraph.spawner.inline_diff.subprocess.run',
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout='+change\n', stderr=''))
    # Admission itself must never refuse; only inline prompt assembly requires a diff.
    monkeypatch.setattr('plangraph.spawner.context_pack.annotate', lambda *a: None)
    seat._annotate_spawn(dict(role='review', plan={'p_source': 'override'}),
                         'Review', card, str(tmp_path), SimpleNamespace(first_leg=chosen), False)
    if mode == 'inline-only' and not has_diff:
        with pytest.raises(InlineDiffError, match='no diff_ref'):
            seat._leg_prompt('Review', card, 'review', chosen)
    else:
        result = seat._leg_prompt('Review', card, 'review', chosen)
        assert ('+change' in result) if mode == 'inline-only' else result == 'Review'


def test_warning_uses_record_not_selector(monkeypatch):
    seat = SeatSpawnMixin()
    monkeypatch.setattr('plangraph.spawner.context_pack.annotate', lambda *a: None)
    record = dict(role='review', plan={'p_source': 'override'},
                  chosen_leg={'model': 'mimo/mimo-v2.6-flash'})
    seat._annotate_spawn(record, '', {}, None, SimpleNamespace(first_leg=DEFAULT_LEGS[0]), False)
    assert record['warnings'] == ['review_unfit: mimo/mimo-v2.6-flash']


@pytest.mark.parametrize('harness,mode,inline', [('qodercli', 'tools', False),
                                               ('opencode', 'tools', True),
                                               ('opencode', 'inline-only', True)])
def test_diff_delivery(monkeypatch, harness, mode, inline):
    """Only harnesses with repository tools can omit the inline diff."""
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
    """No-regression guard: existing seat exclusion and diff_ref refusal."""
    from plangraph.spawner.inline_diff import InlineDiffError
    leg = replace(DEFAULT_LEGS[0], model='opencode-go/qwen3.8-flash')
    assert RoutePolicy().skip_reason(leg, 'review') == 'no_verdict_seat'
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=None)
    with pytest.raises(InlineDiffError, match='no diff_ref'):
        seat._leg_prompt('Review', {}, 'review', replace(leg, tool_mode='inline-only'))
