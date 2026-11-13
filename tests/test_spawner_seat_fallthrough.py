"""Replay the relevant attempts from run-cb082cf01dc5429f8c3d74c70595b5c0."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from test_spawner_review_tools_inline_launch_boundary import launch_leg_case  # noqa: F401
from plangraph.planner.legs import Leg
from plangraph.planner.routing import SpawnPlan
from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner.env_redact import first_reason
from plangraph.spawner.exit_outcome import failure_reason, terminal_reason
from plangraph.spawner.run_watchdog import Watchdog, leg_limits, thresholds
from plangraph.spawner.runner import spawn_service
from plangraph.spawner.service import SpawnService


OOM = '{"plangraph_oom": {"event": "oom_score_adj", "value": 500}}'
INVALID = ('error: invalid model selection (--model "claude-opus-4-6-thinking" --effort ""): '
           'model claude-opus-4-6-thinking is not recognized as a known model or custom model in settings')


@pytest.fixture
def recorded_plan():
    # Projection copied from the private record: no host, paths or credentials.
    skipped = tuple(dict(leg=leg, reason=reason) for leg, reason in (
        ('mimo-v2.6-pro', 'excluded_family'),
        ('opencode-gemini-4-argon', 'limited'),
        ('omniroute-deepseek-flash', 'not_served_on_host: omniroute/deepseek-flash'),
        ('agy-gemini-4-argon', 'unknown')))
    rows = (
        ('agy-claude-opus', 'agy', 'claude-opus-4-6-thinking', 'claude'),
        ('agy-opus-5.5', 'agy', 'claude-opus-5-5-high', 'claude'),
        ('opencode-go-gemini-4-argon', 'opencode', 'opencode-go/gemini-4-argon', 'gemini'),
        ('opencode-go-deepseek-v4-flash', 'opencode', 'opencode-go/deepseek-v4.1-flash', 'deepseek'),
        ('claude-haiku-5.5', 'claude', 'claude-haiku-5-5', 'claude'))
    legs = tuple(Leg(name, 'subscription', harness, model, family, ('128k',),
                     131072, False, roles=('review', 'implement'))
                 for name, harness, model, family in rows)
    return SpawnPlan(legs, skipped, 'recorded-attempts', (), ())


@pytest.fixture(params=['base', 'routed'])
def replay(request, tmp_path, monkeypatch, recorded_plan):
    clock, calls, budgets, run_ids, active = [1000.0], [], [], [], []
    monkeypatch.setattr('time.time', lambda: clock[0])
    runner = NS(_processes={}, _tasks={}, jobs={}, provider_responses={},
                default_cwd=str(tmp_path))
    sink = NS(deliver=lambda *a: None)
    options = dict(runner=runner, state=tmp_path / 'state',
                   leg_reader=LegStateStore(tmp_path / 'legs.sqlite'))
    service = (SpawnService(**options) if request.param == 'base' else
               spawn_service(**options, completion_sink=sink, keeper_sink=sink))
    monkeypatch.setattr('plangraph.spawner.context_pack.annotate', lambda *a: None)
    monkeypatch.setattr(service, '_leg_prompt', lambda *a, **kw: 'review')
    monkeypatch.setattr(service, '_route', lambda *a, **kw: recorded_plan)

    async def admitted(plan, **kwargs):
        return plan

    async def no_probe(*args):
        return None

    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', admitted)
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', no_probe)

    def run(*, succeed=None, role='review', no_fallthrough=False, duration=2,
            max_s=60, terminal=None, failures=None, limited=None, provider_stop=False):
        async def spawn(**kwargs):
            index = len(calls)
            chosen = next(l for l in recorded_plan.legs if l.model == kwargs['model_or_combo'])
            calls.append(chosen.id)
            record = kwargs['prompt_record'][1]
            run_ids.append(record['run_id'])
            if index:
                previous = service.result(record['run_id'])
                active.append((previous['status'], previous['ended']))
                assert any(a.get('leg', {}).get('id') == recorded_plan.legs[index - 1].id
                           for a in previous['attempts'] if isinstance(a.get('leg'), dict))
            budgets.append(Watchdog(kwargs['watchdog'], clock[0], None, writer=False).budget())
            ok = chosen.id == succeed
            stderr = '' if ok else ((failures or {}).get(chosen.id) or
                                     (OOM + '\n' + INVALID if index == 0 else
                                      'error: transport failed for ' + chosen.id))
            status = terminal if terminal and index == 0 else 'completed' if ok else 'failed'
            if chosen.id == limited:
                status = 'limited'
            events = [{'event': terminal}] if terminal == 'model_refusal' else []
            job = NS(job_id=chosen.id, status=status, exit_code=0 if ok else 1,
                     stdout='VERDICT: pass\n' if ok else '', stderr=stderr,
                     served_model=chosen.model, served_family=chosen.family,
                     served_witness_reason='fixture', events=events, survivors=[],
                     request_side_model=chosen.model, witness_kind='fixture', role=role)
            runner.jobs[job.job_id] = job
            if provider_stop and index == 0:
                job.status, job.exit_code = 'completed', 0
                runner.provider_responses[job.job_id] = {'status': 429, 'body': 'rate limited'}
            clock[0] += duration
            return job

        runner.spawn = spawn
        return asyncio.run(service._spawn(card={'title': 'recorded replay', 'max_min': max_s / 60},
            role=role, wait_seconds=None, no_fallthrough=no_fallthrough, _plan=recorded_plan))

    return NS(run=run, calls=calls, budgets=budgets, run_ids=run_ids, active=active,
              plan=recorded_plan)


def attempts_by_leg(record):
    return {a['leg']['id'] if isinstance(a['leg'], dict) else a['leg']: a
            for a in record['attempts']}


def test_recorded_failed_leg_starts_next_in_same_run(replay):
    record = replay.run(succeed='agy-opus-5.5')
    assert replay.calls == ['agy-claude-opus', 'agy-opus-5.5']
    assert len(set(replay.run_ids)) == 1
    assert replay.active == [('running', None)]
    assert record['status'] == 'completed' and record['chosen_leg']['id'] == 'agy-opus-5.5'
    attempts = attempts_by_leg(record)
    assert attempts['agy-claude-opus']['skip_reason'] == INVALID
    for row in replay.plan.skipped:
        assert attempts[row['leg']]['skip_reason'] == row['reason']
    assert all(a.get('skip_reason') for a in record['attempts'] if a['status'] != 'completed')
    assert all(attempts[l.id]['skip_reason'] == 'earlier_leg_succeeded' for l in replay.plan.legs[2:])
    assert 'reason' not in record


def test_recorded_run_fails_only_after_every_leg_is_tried(replay):
    record = replay.run()
    assert replay.calls == [l.id for l in replay.plan.legs]
    assert replay.active == [('running', None)] * 4
    assert record['status'] == 'failed' and record['exit_code'] != 0
    attempts = attempts_by_leg(record)
    assert all(attempts[l.id]['status'] == 'failed' for l in replay.plan.legs)
    assert all(a['skip_reason'] and 'plangraph_oom' not in a['skip_reason']
               and a['skip_reason'] != 'run_terminated_before_leg' for a in record['attempts'])
    assert 'plangraph_oom' not in record['reason']


def test_recorded_route_continues_through_limit_and_not_served(replay):
    record = replay.run(succeed='claude-haiku-5.5', limited='agy-opus-5.5', failures={
        'agy-opus-5.5': 'error: usage limit reached',
        'opencode-go-gemini-4-argon': 'not_served_on_host: opencode-go/gemini-4-argon'})
    assert replay.calls == [l.id for l in replay.plan.legs]
    attempts = attempts_by_leg(record)
    assert attempts['agy-opus-5.5']['skip_reason'] == 'harness_limit: error: usage limit reached'
    assert attempts['opencode-go-gemini-4-argon']['skip_reason'].startswith('not_served_on_host:')
    assert record['status'] == 'completed' and 'reason' not in record


def test_fallthrough_uses_one_run_deadline(replay):
    record = replay.run(duration=4, max_s=6)
    assert replay.calls == ['agy-claude-opus', 'agy-opus-5.5']
    assert replay.budgets == [6, 2]
    assert record['status'] == 'timed_out' and record['reason'] == 'timeout'
    assert record['exit_code'] == 124


def test_provider_limit_is_failed_attempt_even_with_completed_job(replay):
    record = replay.run(succeed='agy-opus-5.5', provider_stop=True)
    assert replay.calls == ['agy-claude-opus', 'agy-opus-5.5']
    attempt = attempts_by_leg(record)['agy-claude-opus']
    assert attempt['status'] == 'failed' and attempt['skip_reason'] == 'provider_stop'
    assert record['status'] == 'completed'


@pytest.mark.parametrize('role,no_fallthrough', [('implement', False), ('review', True)])
def test_writer_and_no_fallthrough_keep_their_stop_rule(replay, role, no_fallthrough):
    record = replay.run(role=role, no_fallthrough=no_fallthrough)
    assert replay.calls == ['agy-claude-opus']
    assert record['status'] == 'failed'


@pytest.mark.parametrize('terminal', ['timeout', 'stalled', 'cancelled', 'model_refusal'])
def test_terminal_stops_are_not_retried(replay, terminal):
    record = replay.run(terminal=terminal)
    assert replay.calls == ['agy-claude-opus']
    assert record['status'] != 'completed'


@pytest.mark.parametrize('event', ['oom_score_adj', 'oom_score_adj_denied', 'oom_score_adj_failed'])
@pytest.mark.parametrize('diagnostic', ['', '\nerror: transport failed'])
def test_oom_markers_never_supply_failure_reasons(event, diagnostic):
    text = '\x1b[0m\n' + OOM.replace('oom_score_adj', event) + diagnostic
    assert first_reason(text) == diagnostic.strip()
    job = NS(stderr=text, exit_code=1)
    assert failure_reason(job) == (diagnostic.strip() or 'harness exit code 1')
    record = dict(role='review', status='failed', exit_code=1, stderr_tail=text)
    terminal_reason(record)
    assert record['reason'] == (diagnostic.strip() or 'harness exit 1')


def test_launch_overhead_cannot_extend_shared_deadline(monkeypatch):
    monkeypatch.setattr('time.time', lambda: 1002)
    limits = leg_limits(dict(role='review', started=1000), thresholds({'max_min': 0.1}, role='review'))
    watchdog = Watchdog(limits, 1004, None, writer=False)
    assert watchdog.budget() == 2
    assert watchdog.decide(1006, None, None, None)['rule'] == 'max'


def test_reasoning_retry_uses_absolute_deadline(launch_leg_case, monkeypatch):
    # Reuse the shared launch-boundary fixture without a real child.
    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('time.time', lambda: 110)
    jobs[1].events = []
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None,
        watchdog={'max_s': 40, 'deadline': 150, 'hard_max': True}))
    assert len(calls) == 2
    assert calls[1]['watchdog']['max_s'] == 40
    assert calls[1]['watchdog']['deadline'] == 150
