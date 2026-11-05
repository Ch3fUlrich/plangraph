"""Synthetic runtime-boundary regressions; no live acceptance or savings claim."""
import asyncio
import builtins
import io
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace as NS

import pytest

from plangraph.spawner import env_redact, exit_outcome, run_watchdog
from plangraph.routing_support.leg_state import LegStateStore


LINES = ['Error: server busy', 'Error: too busy', 'Error: overloaded',
         'HTTP 503: Service Unavailable']
ENVELOPE = json.dumps({'autoos_provider_response': {'status': 503,
                                                   'body': 'Service Unavailable'}})
CASES = LINES + ['\x1b[31m' + s + '\x1b[0m' for s in LINES] + [ENVELOPE]


@pytest.fixture(autouse=True)
def hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr('os.environ', {})
    def denied(*args, **kwargs):
        raise AssertionError('external access denied')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket.socket, 'sendto', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)
    monkeypatch.setattr(Path, 'home', denied)
    original = builtins.open
    def local_open(file, *args, **kwargs):
        if not isinstance(file, int):
            assert Path(file).resolve().is_relative_to(tmp_path)
        return original(file, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', local_open)
    monkeypatch.setattr(io, 'open', local_open)


@pytest.mark.parametrize('line', CASES)
def test_live_collect_fails_then_next_eligible(line, monkeypatch, tmp_path):
    # Real collector and streams, injected process/clock/reaper, no subprocess.
    async def exercise():
        clock = [1000.0]
        monkeypatch.setattr(run_watchdog.time, 'time', lambda: clock[0])
        async def tick(gate):
            while not gate.refused.is_set():
                await asyncio.sleep(0)
                clock[0] += 1
                gate.poll(clock[0])
        monkeypatch.setattr(run_watchdog.RefusalGate, 'run', tick)
        proc = NS(stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader(),
                  returncode=None)
        proc.stderr.feed_data((line + '\n').encode())
        job = NS(start_time=1000, role='review', status='running', events=[],
                 last_activity_ts=None, survivors=[], cwd=None, stdout='', stderr='')
        reaped = []
        async def reap(*args):
            reaped.append(True)
            proc.returncode = 7
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
        runner = NS(jobs={'fixture': job}, _child_envs={'fixture': {}},
                    _stdin_payloads={}, _reap_process=reap)
        await asyncio.wait_for(run_watchdog.collect(runner, 'fixture', proc), 2)
        assert (job.status, job.exit_code) == ('failed', 7)
        assert len(reaped) == 1
        assert [e['event'] for e in job.events] == ['dispatch_refused']
        store = LegStateStore(tmp_path / 'legs.sqlite')
        notifications = []
        service = NS(leg_reader=store.leg_status,
                     completion_sink=NS(deliver=lambda *a: notifications.append(a)))
        assert exit_outcome.fallthrough_reason(service, NS(model='dead'), job, {'run_id': 'fixture'})
        assert store.leg_status('dead')['state'] == 'limited'
        assert store.leg_status('next')['state'] == 'ok'
        assert len(notifications) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('line', CASES)
def test_exit_replay_cooldown_and_probe(line, monkeypatch, tmp_path):
    clock = [1000.0]
    monkeypatch.setattr('plangraph.routing_support.leg_state.time.time', lambda: clock[0])
    store = LegStateStore(tmp_path / 'legs.sqlite')
    notices = []
    service = NS(leg_reader=store.leg_status,
                 completion_sink=NS(deliver=lambda *a: notices.append(a)))
    attempts = []
    # Before: a reader without refusal evidence dispatches all four choices.
    before = sum(store.leg_status('dead')['state'] == 'ok' for _ in range(4))
    for _ in range(4):
        chosen = next(m for m in ('dead', 'next') if store.leg_status(m)['state'] == 'ok')
        attempts.append(chosen)
        job = NS(exit_code=1 if chosen == 'dead' else 0, stdout=line, stderr='')
        reason = exit_outcome.fallthrough_reason(service, NS(model=chosen), job, {'run_id': 'fixture'})
        assert bool(reason) == (chosen == 'dead')
    assert before == 4
    assert attempts == ['dead', 'next', 'next', 'next']
    assert len(notices) == 1
    evidence = store.leg_status('dead')['evidence']
    assert evidence['event_id'] == 1
    assert 60 <= evidence['reset_at'] - clock[0] <= 86400
    clock[0] = evidence['reset_at'] + 1
    assert store.leg_status('dead')['state'] == 'ok'
    store.record_probe_pass('dead', 'fixture')
    assert store.leg_status('dead')['state'] == 'ok'


@pytest.mark.parametrize('text', LINES + [ENVELOPE, 'We explain server busy and HTTP 503 here.',
                                         'Error: the parser handles overloaded responses.'])
def test_success_and_prose_guards(text):
    assert env_redact.harness_limit_line(0, text, text) is None
    assert env_redact.unavailable_reason(0, text, text) is None
    if text.startswith(('We ', 'Error: the')):
        assert env_redact.early_refusal(text) is None
        assert env_redact.harness_limit_line(1, text, text) is None


def test_quiet_guard_and_provider_reset(monkeypatch, tmp_path):
    from plangraph.spawner.run_refusal import RefusalGate
    gate = RefusalGate(NS(is_set=lambda: False), NS(start_time=1000), lambda s: s)
    gate.candidate(env_redact.early_refusal(LINES[0]), 1001)
    gate.activity(1002)
    gate.poll(1010)
    assert gate.pending is None  # later healthy activity still cancels refusal
    monkeypatch.setattr('plangraph.routing_support.leg_state.time.time', lambda: 1000)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    line = 'Error: server busy (reset after 2h)'
    exit_outcome.fallthrough_reason(NS(leg_reader=store), NS(model='dead'),
                                   NS(exit_code=1, stdout='', stderr=line), {})
    assert store.leg_status('dead')['evidence']['reset_at'] == 8200
