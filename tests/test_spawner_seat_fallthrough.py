"""Replay the relevant attempts from run-cb082cf01dc5429f8c3d74c70595b5c0."""
import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from plangraph.planner.legs import Leg
from plangraph.planner.routing import SpawnPlan
from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner.env_redact import first_reason
from plangraph.spawner.exit_outcome import failure_reason, terminal_reason
from plangraph.spawner.run_watchdog import Watchdog, leg_limits, thresholds
from plangraph.spawner.runner import spawn_service
from plangraph.spawner.service import SpawnService


OOM = '{"plangraph_oom": {"event": "oom_score_adj", "value": 500}}'
CAPTURE = json.loads((Path(__file__).parent / 'fixtures/spawner_seat_fallthrough.json').read_text())
FAILED = next(a for a in CAPTURE['attempts'] if a['status'] == 'failed')
INVALID = FAILED['stderr_tail'].splitlines()[1]


@pytest.fixture
def recorded_plan():
    # Replay at the launch boundary: retain the actual pre-launch skips, then
    # resume the recorded route at its failed job. Later outcomes are injected.
    prelaunch = [a for a in CAPTURE['attempts'] if a['skip_reason'] == 'unknown']
    skipped = tuple(CAPTURE['skipped']) + tuple(dict(leg=a['leg'], reason=a['skip_reason']) for a in prelaunch)
    omitted = {a['leg'] for a in prelaunch}
    legs = tuple(Leg(**dict(row, max_request_tokens=131072))
                 for row in CAPTURE['legs'] if row['id'] not in omitted)
    return SpawnPlan(legs, skipped, 'recorded-attempts', (), ())


@pytest.fixture(params=['base', 'routed'])
def replay(request, tmp_path, monkeypatch, recorded_plan):
    clock, calls, budgets, run_ids, active, saves = [time.time()], [], [], [], [], []
    monkeypatch.setattr('time.time', lambda: clock[0])
    runner = NS(_processes={}, _tasks={}, jobs={}, provider_responses={},
                default_cwd=str(tmp_path))
    sink = NS(deliver=lambda *a: None)
    options = dict(runner=runner, state=tmp_path / 'state',
                   leg_reader=LegStateStore(tmp_path / 'legs.sqlite'))
    service = (SpawnService(**options) if request.param == 'base' else
               spawn_service(**options, completion_sink=sink, keeper_sink=sink))
    save = service._save

    def capture_save(record):
        save(record)
        saves.append((record['status'], record.get('ended')))

    monkeypatch.setattr(service, '_save', capture_save)
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
             max_s=60, terminal=None, failures=None, limited=None, provider_stop=False,
             refusal_event=False, prepare_s=0, seat=False, include_skips=True,
             dry_run=False, stop_status=429, refusal_reason=None, initial_s=0):
        if initial_s:
            from plangraph.spawner import run_watchdog
            original = run_watchdog.leg_limits

            def delay_first(record, limits):
                clock[0] += initial_s
                return original(record, limits)

            monkeypatch.setattr(run_watchdog, 'leg_limits', delay_first)
        async def prepare(*args, **kwargs):
            if max_s is None:
                assert kwargs['deadline'] is None
            clock[0] += prepare_s
            return 'review'

        monkeypatch.setattr(service, '_prepare_leg_prompt', prepare)
        async def spawn(**kwargs):
            index = len(calls)
            chosen = next(l for l in recorded_plan.legs if l.model == kwargs['model_or_combo'])
            calls.append(chosen.id)
            record = kwargs['prompt_record'][1]
            run_ids.append(record['run_id'])
            if index:
                previous = service.result(record['run_id'])
                active.append((previous['status'], previous['ended']))
                assert previous['exit_code'] is None
                assert any(a.get('leg', {}).get('id') == recorded_plan.legs[index - 1].id
                           for a in previous['attempts'] if isinstance(a.get('leg'), dict))
            budgets.append(Watchdog(kwargs['watchdog'], clock[0], None, writer=False).budget())
            ok = chosen.id == succeed or dry_run
            stderr = '' if ok else ((failures or {}).get(chosen.id) or
                                      (FAILED['stderr_tail'] if index == 0 else
                                       'error: transport failed for ' + chosen.id))
            status = terminal if terminal and index == 0 else 'dry_run' if dry_run else 'completed' if ok else 'failed'
            if chosen.id == limited:
                status = 'limited'
            events = ([dict(event='model_refusal', stop_reason='refusal', refusal_text_chars=12)]
                      if refusal_event or terminal == 'model_refusal' else CAPTURE['events'] if index == 0 else [])
            if refusal_reason is not None:
                record['reason'] = refusal_reason
            job = NS(job_id=chosen.id, status=status, exit_code=0 if ok else 1,
                     stdout='VERDICT: pass\n' if ok else '', stderr=stderr,
                     served_model=chosen.model, served_family=chosen.family,
                     served_witness_reason='fixture', events=events, survivors=[],
                     request_side_model=chosen.model, witness_kind='fixture', role=role)
            runner.jobs[job.job_id] = job
            if provider_stop and index == 0:
                job.status, job.exit_code = provider_stop, 0 if provider_stop == 'completed' else 1
                runner.provider_responses[job.job_id] = {'status': stop_status, 'body': 'rate limited'}
            Path(kwargs['live_log']).write_text(stderr)
            clock[0] += duration
            return job

        runner.spawn = spawn
        card = {'title': 'recorded replay'}
        if max_s is None:
            monkeypatch.setattr('plangraph.spawner.run_watchdog.thresholds',
                                lambda *a, **kw: dict(thresholds(role=role), max_s=None))
        else:
            card['max_min'] = max_s / 60
        plan = recorded_plan if include_skips else replace(recorded_plan, skipped=())
        if seat:
            plan = replace(plan, seat_coverage=(dict(leg=plan.legs[0].id, seat_group='seats-fixture'),))
        return asyncio.run(service._spawn(card=card,
            role=role, wait_seconds=None, no_fallthrough=no_fallthrough, _plan=plan, dry_run=dry_run))

    return NS(run=run, calls=calls, budgets=budgets, run_ids=run_ids, active=active,
              plan=recorded_plan, service=service, saves=saves)


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
    assert attempts['agy-claude-opus']['stderr_tail'] == FAILED['stderr_tail']
    assert CAPTURE['events'][0] in record['events']
    for row in replay.plan.skipped:
        assert attempts[row['leg']]['skip_reason'] == row['reason']
    assert all(a.get('skip_reason') for a in record['attempts'] if a['status'] != 'completed')
    assert all(attempts[l.id]['skip_reason'] == 'earlier_leg_succeeded' for l in replay.plan.legs[2:])
    assert 'reason' not in record


def test_recorded_run_fails_only_after_every_leg_is_tried(replay):
    record = replay.run()
    assert replay.calls == [l.id for l in replay.plan.legs]
    assert replay.active == [('running', None)] * (len(replay.plan.legs) - 1)
    assert record['status'] == 'failed' and record['exit_code'] != 0
    attempts = attempts_by_leg(record)
    assert all(attempts[l.id]['status'] == 'failed' for l in replay.plan.legs)
    assert all(a['skip_reason'] and 'plangraph_oom' not in a['skip_reason']
               and a['skip_reason'] != 'run_terminated_before_leg' for a in record['attempts'])
    reason = record.get('reason')
    assert reason and 'plangraph_oom' not in reason


def test_recorded_route_continues_through_limit_and_not_served(replay):
    record = replay.run(succeed='claude-haiku-5.5', limited='agy-opus-5.5', failures={
        'agy-opus-5.5': 'error: usage limit reached',
        'opencode-go-gemini-4-argon': 'not_served_on_host: opencode-go/gemini-4-argon'})
    assert replay.calls == [l.id for l in replay.plan.legs]
    attempts = attempts_by_leg(record)
    assert attempts['agy-opus-5.5']['skip_reason'] == 'harness_limit: error: usage limit reached'
    assert attempts['opencode-go-gemini-4-argon']['skip_reason'].startswith('not_served_on_host:')
    assert record['status'] == 'completed' and 'reason' not in record


@pytest.mark.parametrize('prepare_s,initial_s', [(0, 0), (7, 0), (0, 7)])
def test_fallthrough_uses_one_run_deadline(replay, prepare_s, initial_s):
    result = replay.run(duration=4, max_s=6, prepare_s=prepare_s, initial_s=initial_s)
    record = json.loads(replay.service._path(result['run_id']).read_text())
    assert replay.calls == ([] if prepare_s or initial_s else ['agy-claude-opus', 'agy-opus-5.5'])
    assert replay.budgets == ([] if prepare_s or initial_s else [6, 2])
    assert record['status'] == 'timed_out' and record['reason'] == 'timeout'
    assert record['exit_code'] == 124
    assert ('timed_out', None) in replay.saves
    assert {'event': 'run_timeout'} in record['events']
    assert record['ended'] is not None and record['live_log'].endswith('/live.log')
    if not (prepare_s or initial_s):
        assert 'transport failed' in Path(record['live_log']).read_text()


@pytest.mark.parametrize('diagnostic', [INVALID, 'usage_limit: task diagnostic', 'credit_limit: task diagnostic'])
def test_ordinary_review_failure_has_no_health_side_effects(replay, monkeypatch, diagnostic):
    from plangraph.planner.measured_down import measured_down
    from plangraph.spawner import exit_outcome
    from plangraph.spawner.leg_probe import key
    effects = []
    monkeypatch.setattr(exit_outcome, '_book_failure', lambda *a: effects.append('book'))
    monkeypatch.setattr(exit_outcome, 'notify_leg_refused', lambda *a: effects.append('notify'))
    record = replay.run(succeed='agy-opus-5.5', failures={'agy-claude-opus': diagnostic}, include_skips=False)
    attempt = attempts_by_leg(record)['agy-claude-opus']
    assert attempt['skip_reason'] == diagnostic and attempt.get('down_reason') is None
    assert replay.service.leg_reader.leg_status(key(replay.plan.legs[0]))['state'] == 'ok'
    assert measured_down(replay.service.state) == {} and effects == []
    assert replay.calls == ['agy-claude-opus', 'agy-opus-5.5']


@pytest.mark.parametrize('provider_stop', [False, 'failed'])
@pytest.mark.parametrize('stop_status', [402, 429])
def test_failed_review_with_model_refusal_event_stops(replay, provider_stop, stop_status):
    record = replay.run(refusal_event=True, provider_stop=provider_stop, stop_status=stop_status)
    assert replay.calls == ['agy-claude-opus']
    assert record['status'] == 'failed' and record['reason'] == 'model_refusal'
    assert attempts_by_leg(record)['agy-claude-opus']['skip_reason'] == 'model_refusal'
    assert dict(event='model_refusal', stop_reason='refusal', refusal_text_chars=12) in record['events']


@pytest.mark.parametrize('status', ['completed', 'failed'])
def test_provider_limit_is_failed_attempt_even_with_completed_job(replay, status):
    # D-5657: completed harness jobs now retain their status even on provider stops.
    record = replay.run(succeed='agy-opus-5.5', provider_stop=status, seat=True)
    assert replay.calls == ['agy-claude-opus', 'agy-opus-5.5']
    attempt = attempts_by_leg(record)['agy-claude-opus']
    assert attempt['status'] == status and attempt['skip_reason'] == 'provider_stop'
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


@pytest.mark.parametrize('prefix', ['', 'wrapper: '])
@pytest.mark.parametrize('event', ['oom_score_adj', 'oom_score_adj_denied', 'oom_score_adj_failed'])
@pytest.mark.parametrize('diagnostic', ['', '\nerror: transport failed'])
def test_oom_markers_never_supply_failure_reasons(event, diagnostic, prefix):
    text = '\x1b[0m\n' + prefix + OOM.replace('oom_score_adj', event) + diagnostic
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


@pytest.fixture
def launch_leg_case(tmp_path):
    from plangraph.spawner.runner import JobInfo
    from plangraph.spawner.seat_spawn import SeatSpawnMixin
    chosen = Leg('fixture', 'subscription', 'opencode', 'fixture/model', 'fixture',
                 ('128k',), 131072, False, roles=('review',))
    jobs = [JobInfo(name, 'failed', 'review', chosen.model, role='review', exit_code=1,
                    events=[dict(event='opencode_reasoning_exhausted', cap_available=True)])
            for name in ('first', 'second')]
    calls, seat = [], SeatSpawnMixin()
    seat.runner = NS(_processes={}, _tasks={}, _owned={}, default_cwd=str(tmp_path))
    seat._running_records = {}
    seat._path = lambda run_id: tmp_path / 'record.json'
    seat._save = lambda record: seat._path(record['run_id']).write_text(json.dumps(record))
    record = dict(run_id='run-test', role='review', status='running', started=100,
                  attempts=[], events=[], writer_family='original')

    async def spawn(**options):
        job = jobs[len(calls)]
        calls.append(options)
        return job

    seat.runner.spawn = spawn
    return seat, record, chosen, jobs, calls


@pytest.mark.parametrize('max_s,deadline', [(40, 150), (None, None)])
def test_reasoning_retry_uses_absolute_deadline(launch_leg_case, monkeypatch, max_s, deadline):
    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('time.time', lambda: 110)
    jobs[1].events = []
    limits = dict(max_s=max_s, hard_max=True)
    if deadline is not None:
        limits['deadline'] = deadline
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog=limits))
    assert len(calls) == 2
    assert calls[1]['watchdog']['max_s'] == max_s
    assert calls[1]['watchdog'].get('deadline') == deadline


def test_review_without_numeric_cap_has_no_deadline(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', '')
    limits = dict(thresholds(role='review'), max_s=None)
    record = dict(role='review', started=1000)
    assert leg_limits(record, limits) is limits and 'deadline' not in limits
    assert Watchdog(limits, 1000, None, writer=False).decide(100000, None, None, None) is None
    assert record == dict(role='review', started=1000)


def test_empty_review_timeout_keeps_ordinary_maximum(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', '')
    monkeypatch.setattr('time.time', lambda: 1000)
    limits = thresholds(role='review')
    assert limits['max_s'] == 90 * 60
    assert leg_limits(dict(role='review', started=1000), limits)['deadline'] == 6400


def test_uncapped_review_dispatch_can_fall_through(replay):
    record = replay.run(succeed='agy-opus-5.5', max_s=None)
    assert record['status'] == 'completed' and replay.budgets == [float('inf')] * 2


@pytest.mark.parametrize('status', ['limited', 'skipped'])
@pytest.mark.parametrize('stop_status', [402, 429, 503])
def test_specific_unavailable_reason_outranks_provider_stop(replay, status, stop_status):
    diagnostic = 'error: model unavailable: fixture/model'
    record = replay.run(succeed='agy-opus-5.5', provider_stop=status, stop_status=stop_status,
                        failures={'agy-claude-opus': diagnostic})
    attempt = attempts_by_leg(record)['agy-claude-opus']
    assert attempt['status'] == 'failed' and attempt['skip_reason'] == diagnostic
    assert attempt['provider_response']['status'] == stop_status
    assert record['status'] == 'completed'


@pytest.mark.parametrize('dry_run,no_fallthrough,provider_stop,expected', [
    (True, False, False, 'dry_run'), (True, False, 'completed', 'completed'),
    (False, True, 'failed', 'failed'), (False, False, 'completed', 'completed')])
def test_review_continuation_status_matches_dispatch_paths(replay, dry_run, no_fallthrough, provider_stop, expected):
    record = replay.run(succeed='agy-opus-5.5', dry_run=dry_run,
                        no_fallthrough=no_fallthrough, provider_stop=provider_stop)
    assert record['status'] == expected and record['ended'] is not None
    if dry_run or no_fallthrough:
        assert replay.calls == ['agy-claude-opus'] and replay.active == []
    else:
        assert replay.calls == ['agy-claude-opus', 'agy-opus-5.5']
        assert replay.active == [('running', None)]


@pytest.mark.parametrize('role', ['review', 'implement'])
@pytest.mark.parametrize('reason', [None, 'cancelled', 'git_config_mutated', 'other_diagnostic'])
def test_dispatch_safety_refusal_preserves_distinct_terminal_reason(replay, monkeypatch, role, reason):
    from plangraph.spawner import exit_outcome
    effects = []
    monkeypatch.setattr(exit_outcome, '_book_failure', lambda *a: effects.append('book'))
    monkeypatch.setattr(exit_outcome, 'notify_leg_refused', lambda *a: effects.append('notify'))
    record = replay.run(role=role, refusal_event=True, refusal_reason=reason, provider_stop='failed')
    assert replay.calls == ['agy-claude-opus'] and effects == []
    assert record['status'] == 'failed' and record['exit_code'] != 0
    assert record['reason'] == (reason or 'model_refusal')
    assert attempts_by_leg(record)['agy-claude-opus']['skip_reason'] == (reason or 'model_refusal')
    assert 'continuation_run_id' not in record


def test_recorded_capture_matches_registered_provider_identity(recorded_plan):
    from plangraph.planner.legs import DEFAULT_LEGS
    registered = next(leg for leg in DEFAULT_LEGS if leg.id == 'omniroute-deepseek-flash')
    skipped = next(row for row in CAPTURE['skipped'] if row['leg'] == registered.id)
    assert skipped['reason'] == 'not_served_on_host: ' + registered.model
    assert registered.model == 'omniroute/deepseek-flash'
    unlaunched = [a['leg'] for a in CAPTURE['attempts'] if a['skip_reason'] == 'run_terminated_before_leg']
    assert unlaunched == [leg.id for leg in recorded_plan.legs[1:]]
    assert [a['leg'] for a in CAPTURE['attempts'] if a['skip_reason'] == 'unknown'] == [
        'agy-gemini-4-argon', 'agy-sonnet-5.5']
