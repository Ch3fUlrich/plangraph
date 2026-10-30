"""D-3226: response-side served-model witness from the OmniRoute call log (sessionTag)."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner import gateway_witness as gw


class _Runner:
    def __init__(self, job_id):
        self._adapter_tokens = {job_id: object()}
        self.calls = []

    def served_witness(self, job_id, model, *, adapter_token):
        if adapter_token is not self._adapter_tokens.get(job_id):
            raise ValueError('invalid adapter token')
        if '/' not in model:
            raise ValueError('served model is not an admitted model id: ' + model)
        self.calls.append(model)


def _job(model='omniroute/cx-gpt-6-luna'):
    return SimpleNamespace(model_or_combo=model, served_model=None, served_family=None, served_witness_reason=None,
                           witness_kind=None, events=[])


def _env(job_id):
    cfg = {'providers': {'omniroute': {'options': {}}}, 'permission': {'*': 'deny'}}
    gw.tag_session(cfg, job_id)
    return {'OPENCODE_CONFIG_CONTENT': json.dumps(cfg)}


def test_tag_session_sets_header_on_omniroute_provider_only():
    cfg = {'provider': {'omniroute': {}, 'mimo': {}}}
    gw.tag_session(cfg, 'job-1')
    # Provider-level headers carry the envelope headers to the gateway (build_child_env).
    assert cfg['provider']['omniroute']['headers'] == {gw.SESSION_HEADER: 'plangraph-job-1'}
    assert cfg['provider']['omniroute']['options']['headers'] == {gw.SESSION_HEADER: 'plangraph-job-1'}
    assert 'options' not in cfg['provider']['mimo'] and 'headers' not in cfg['provider']['mimo']


def test_witness_skips_a_leg_not_served_by_the_gateway():
    # Seat finding (qwen run-5ce67bc0): every config holds an omniroute block, so a mimo leg was probed.
    runner, job = _Runner('job-1'), _job('mimo/mimo-v2.6-pro')
    gw.witness(runner, 'job-1', job, _env('job-1'), fetch=pytest.fail)
    assert not job.events and not runner.calls


def test_witness_names_ambiguous_rows():
    runner, job = _Runner('job-1'), _job()
    rows = [{'sessionTag': 'plangraph-job-1', 'status': 200, 'model': m, 'provider': 'cx'} for m in ('a', 'b')]
    gw.witness(runner, 'job-1', job, _env('job-1'), delay=0, fetch=lambda tag, env: rows)
    assert job.events == [{'event': 'gateway_witness_unavailable',
                           'reason': 'ambiguous: call log rows name 2 served models'}]


def test_served_models_matches_tag_and_ok_status():
    tag = 'plangraph-job-1'
    rows = [{'sessionTag': tag, 'status': 200, 'model': 'gpt-6-luna', 'provider': 'cx'},
            {'sessionTag': 'plangraph-other', 'status': 200, 'model': 'qwen', 'provider': 'x'},
            {'sessionTag': tag, 'status': 502, 'model': 'other', 'provider': 'y'}]
    assert gw.served_models(rows, tag) == {'cx/gpt-6-luna'}
    rows.append({'sessionTag': tag, 'status': 200, 'model': 'mimo/mimo-v2.6-pro'})
    assert gw.served_models(rows, tag) == {'cx/gpt-6-luna', 'mimo/mimo-v2.6-pro'}
    assert gw.served_models({'error': 'x'}, tag) == set()


def test_witness_records_gateway_model(monkeypatch):
    runner, job = _Runner('job-1'), _job()
    rows = [{'sessionTag': 'plangraph-job-1', 'status': 200, 'model': 'gpt-6-luna', 'provider': 'cx'}]
    gw.witness(runner, 'job-1', job, _env('job-1'), delay=0, fetch=lambda tag, env: rows)
    assert runner.calls == ['cx/gpt-6-luna']
    assert job.witness_kind == gw.WITNESS_KIND and not job.events


def test_witness_retries_then_records_absence():
    runner, job = _Runner('job-1'), _job()
    seen = []
    gw.witness(runner, 'job-1', job, _env('job-1'), delay=0, fetch=lambda tag, env: seen.append(tag) or [])
    assert len(seen) == 3 and not runner.calls
    assert job.events == [{'event': 'gateway_witness_unavailable', 'reason': 'no call log row for the session tag'}]


def test_witness_skips_untagged_or_witnessed_jobs():
    runner, job = _Runner('job-1'), _job()
    gw.witness(runner, 'job-1', job, {'OPENCODE_CONFIG_CONTENT': '{}'}, fetch=pytest.fail)
    # r2 seat (mimo-pro run-3b4a1f22): an untagged config is an explicit event, never silent.
    assert job.events == [{'event': 'gateway_witness_unavailable', 'reason': 'final config carries no session tag'}]
    job.events.clear()
    job.served_model = 'cx/gpt-6-luna'
    gw.witness(runner, 'job-1', job, _env('job-1'), fetch=pytest.fail)
    assert not job.events and not runner.calls


def test_fetch_rows_needs_spawner_token_and_sends_it():
    with pytest.raises(LookupError, match=gw.TOKEN_ENV):
        gw.fetch_rows('plangraph-job-1', {}, secrets={})
    sent = []
    def opener(request, timeout):
        sent.append(request)
        return io.BytesIO(b'[{"sessionTag": "plangraph-job-1"}]')
    rows = gw.fetch_rows('plangraph-job-1', {'OMNIROUTE_BASE_URL': 'http://gw:20128/v1'},
                         opener=opener, secrets={gw.TOKEN_ENV: 'tok'})
    assert rows == [{'sessionTag': 'plangraph-job-1'}]
    assert sent[0].full_url.startswith('http://gw:20128/api/usage/call-logs?sessionTag=plangraph-job-1')
    assert sent[0].get_header('Authorization') == 'Bearer tok'


@pytest.mark.parametrize('returncode', [0, 1])   # r2 seat (mimo-pro): a failed seat is witnessed too
def test_runner_review_seat_on_omniroute_is_witnessed_from_call_log(tmp_path, monkeypatch, returncode):
    # Real SpawnerRunner, fake process: the child's final config carries the session header and
    # the runner records the call log's served model as an authenticated adapter witness.
    import asyncio
    from plangraph.spawner import resolver
    from plangraph.spawner.runner import SpawnerRunner
    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    monkeypatch.setattr(gw.time, 'sleep', lambda s: None)
    seen = {}
    def fetch(tag, env):
        seen['tag'] = tag
        return [{'sessionTag': tag, 'status': 200, 'model': 'gpt-6-luna', 'provider': 'cx'}]
    monkeypatch.setattr(gw, 'fetch_rows', fetch)
    async def factory(*args, **kwargs):
        seen['cfg'] = json.loads(kwargs['env'].get('OPENCODE_CONFIG_CONTENT') or '{}')
        async def communicate():
            return b'VERDICT: ACCEPT', b''
        return SimpleNamespace(pid=123, returncode=returncode, communicate=communicate)
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    async def run():
        job = await runner.spawn('prompt', model_or_combo='omniroute/cx-gpt-6-luna', role='review',
                                 client='opencode', cwd=str(tmp_path),
                                 extra_env={'XDG_DATA_HOME': str(tmp_path)}, wait_seconds=3)
        await runner._tasks[job.job_id]
        return job
    job = asyncio.run(run())
    providers = seen['cfg'].get('providers') or seen['cfg'].get('provider')
    assert providers['omniroute']['options']['headers'][gw.SESSION_HEADER] == seen['tag'] == f'plangraph-{job.job_id}'
    assert job.served_model == 'cx/gpt-6-luna' and job.witness_kind == gw.WITNESS_KIND
    assert job.status == ('completed' if returncode == 0 else 'failed')


def test_child_env_never_carries_the_call_log_token(monkeypatch):
    from plangraph.routing_support.clients import build_child_env
    monkeypatch.setenv(gw.TOKEN_ENV, 'tok')
    assert gw.TOKEN_ENV not in build_child_env()
    assert gw.TOKEN_ENV not in build_child_env({gw.TOKEN_ENV: 'tok'})   # extra_env cannot re-add it
