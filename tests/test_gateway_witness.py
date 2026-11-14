"""D-3226: response-side served-model witness from the OmniRoute call log (sessionTag)."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from fake_pid import FAKE_PID
pytestmark = pytest.mark.usefixtures('fake_ownership')   # r3 V3: FAKE_PID exceeds the host PID range; no live process can match

from plangraph.spawner import gateway_witness as gw
from plangraph.planner.legs import DEFAULT_LEGS, EXPLICIT_LEGS


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


def _job(model='omniroute/cx-gpt-6-luna-medium'):
    return SimpleNamespace(model_or_combo=model, served_model=None, served_family=None, served_witness_reason=None,
                           witness_kind=None, events=[])


def _env(job_id):
    cfg = {'providers': {'omniroute': {'options': {}}}, 'permission': {'*': 'deny'}}
    gw.tag_session(cfg, job_id)
    return {'OPENCODE_CONFIG_CONTENT': json.dumps(cfg)}


def test_tag_session_sets_header_on_omniroute_provider_only():
    cfg = {'provider': {'omniroute': {}, 'mimo': {}, 'other': {}}}
    gw.tag_session(cfg, 'job-1')
    # Provider-level headers carry the envelope headers to the gateway (build_child_env).
    assert cfg['provider']['omniroute']['headers'] == {gw.SESSION_HEADER: 'plangraph-job-1'}
    assert cfg['provider']['omniroute']['options']['headers'] == {gw.SESSION_HEADER: 'plangraph-job-1'}
    assert cfg['provider']['mimo']['headers'] == {gw.SESSION_HEADER: 'plangraph-job-1'}
    assert cfg['provider']['mimo']['options']['headers'] == {gw.SESSION_HEADER: 'plangraph-job-1'}
    assert cfg['provider']['other'] == {}


def test_witness_skips_a_leg_not_served_by_the_gateway():
    # Seat finding (qwen run-5ce67bc0): every config holds an omniroute block, so a mimo leg was probed.
    runner, job = _Runner('job-1'), _job('mimo/mimo-v2.6-pro')
    gw.witness(runner, 'job-1', job, _env('job-1'), fetch=pytest.fail)
    assert job.events == [{'event': 'gateway_witness_unavailable', 'reason': 'mimo leg not routed through gateway'}]
    assert not runner.calls


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


@pytest.mark.parametrize('model', ['gpt-6-luna', 'gpt-6-luna-medium'])
def test_witness_records_gateway_model(monkeypatch, model):
    runner, job = _Runner('job-1'), _job('omniroute/cx-' + model)
    rows = [{'sessionTag': 'plangraph-job-1', 'status': 200, 'model': model, 'provider': 'cx'}]
    gw.witness(runner, 'job-1', job, _env('job-1'), delay=0, fetch=lambda tag, env: rows)
    assert runner.calls == ['cx/' + model]
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
    assert job.events == [{'event': 'gateway_witness_unavailable', 'reason': 'served_model already set'}]
    assert not runner.calls


def test_fetch_rows_needs_spawner_token_and_sends_it(tmp_path):
    with pytest.raises(LookupError, match='no ' + gw.TOKEN_ENV):
        gw.fetch_rows('plangraph-job-1', {}, secrets={gw.TOKEN_FILE_ENV: str(tmp_path / 'missing')})
    sent = []
    def opener(request, timeout):
        sent.append(request)
        return io.BytesIO(b'[{"sessionTag": "plangraph-job-1"}]')
    rows = gw.fetch_rows('plangraph-job-1', {'OMNIROUTE_BASE_URL': 'http://gateway.example.invalid:8080/v1'},
                         opener=opener, secrets={gw.TOKEN_ENV: 'tok', 'OMNIROUTE_BASE_URL': 'http://gateway.example.invalid:8080/v1'})
    assert rows == [{'sessionTag': 'plangraph-job-1'}]
    assert sent[0].full_url.startswith('http://gateway.example.invalid:8080/api/usage/call-logs?sessionTag=plangraph-job-1')
    assert sent[0].get_header('Authorization') == 'Bearer tok'


def test_management_url_is_parent_only(monkeypatch):
    monkeypatch.setenv(gw.TOKEN_ENV, 'parent-token')
    monkeypatch.setenv('OMNIROUTE_BASE_URL', 'https://parent.example/v1')
    sent = []

    def opener(request, timeout):
        sent.append(request)
        return io.BytesIO(b'[]')

    child = {'OMNIROUTE_BASE_URL': 'https://child.example/v1', gw.TOKEN_ENV: 'child-token'}
    assert gw.fetch_rows('tag', child, opener=opener) == []
    assert sent[0].full_url.startswith('https://parent.example/api/usage/call-logs?')
    assert sent[0].get_header('Authorization') == 'Bearer parent-token'
    with pytest.raises(LookupError, match='endpoint'):
        gw.fetch_rows('tag', child, secrets={gw.TOKEN_ENV: 'parent-token'}, opener=pytest.fail)


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
        return ([{'sessionTag': seen['expected_tag'], 'status': 200,
                  'model': 'gpt-6-luna', 'provider': 'cx'}]
                if tag == seen['expected_tag'] else [])
    monkeypatch.setattr(gw, 'fetch_rows', fetch)
    async def factory(*args, **kwargs):
        seen['cfg'] = json.loads(kwargs['env'].get('OPENCODE_CONFIG_CONTENT') or '{}')
        seen['expected_tag'] = args[args.index('--title') + 1]
        async def communicate(input=None):
            return (b'VERDICT: ACCEPT' if returncode == 0 else b'seat died, no verdict line'), b''   # D-4430: a final verdict at rc 1 completes
        return SimpleNamespace(pid=FAKE_PID, returncode=returncode, communicate=communicate)
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    async def run():
        job = await runner.spawn('prompt', model_or_combo='omniroute/cx-gpt-6-luna-medium', role='review',
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


def test_child_env_never_carries_the_token_file_path(monkeypatch):
    from plangraph.routing_support.clients import build_child_env
    monkeypatch.setenv(gw.TOKEN_FILE_ENV, '/x/key')
    assert gw.TOKEN_FILE_ENV not in build_child_env()
    assert gw.TOKEN_FILE_ENV not in build_child_env({gw.TOKEN_FILE_ENV: '/x/key'})


def test_key_file_owner_only_check_refuses_windows_modes():
    import stat
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600)
    assert gw._owner_only(info, windows=False)
    assert not gw._owner_only(info, windows=True)  # Windows reports 0o666: never trust the mode there
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o644)
    assert not gw._owner_only(info, windows=False)


_SOL_LEGS = [leg for leg in (*DEFAULT_LEGS, *EXPLICIT_LEGS)
             if leg.id.startswith('cx-gpt-6.1-sol')]


@pytest.mark.parametrize('leg', _SOL_LEGS, ids=lambda leg: leg.id)
@pytest.mark.parametrize('role', ['implement', 'review'])
@pytest.mark.parametrize('row_kind', ['matching', 'prefixed', 'base-model', 'failed-run',
                                      'missing', 'wrong-tag', 'failed', 'unknown-model',
                                      'console-meta/muse-spark-1.3', 'omniroute/resolver-x',
                                      'different-gpt'])
def test_sol_gateway_witness_for_every_route_leg(tmp_path, monkeypatch, leg, role, row_kind):
    import asyncio
    from plangraph.spawner import resolver
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.routing_support import host_keys, opencode_render

    async def probe(*args, **kwargs):
        return None

    def render(env, model):
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps({'provider': {'omniroute': {'options': {}}}})

    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    monkeypatch.setattr(opencode_render, 'render_gateway_providers', render)
    monkeypatch.setattr(host_keys, 'add_host_keys', lambda *a, **kw: None)
    monkeypatch.setattr(gw.time, 'sleep', lambda _: None)
    seen = {}
    served = leg.model.removeprefix('omniroute/cx-')
    if row_kind == 'base-model':
        served = 'gpt-6.1-sol'
    elif row_kind == 'different-gpt':
        served = 'gpt-6-luna'
    elif '/' in row_kind:
        served = row_kind

    def fetch(tag, env):
        seen.setdefault('fetch_tags', []).append(tag)
        if row_kind == 'missing' or tag != seen['expected_tag']:
            return []
        return [{'sessionTag': seen['expected_tag'] if row_kind != 'wrong-tag' else 'another-session',
                 'status': 503 if row_kind == 'failed' else 200,
                 'provider': 'cx',
                 'model': ('unknown-model' if row_kind == 'unknown-model' else
                           'cx/' + served if row_kind == 'prefixed' else served)}]

    monkeypatch.setattr(gw, 'fetch_rows', fetch)

    async def factory(*args, **kwargs):
        seen['env'] = kwargs['env']
        seen['expected_tag'] = args[args.index('--title') + 1]
        async def communicate(input=None):
            return b'VERDICT: ACCEPT', b''
        return SimpleNamespace(pid=FAKE_PID, returncode=1 if row_kind == 'failed-run' else 0,
                               communicate=communicate)

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)

    async def run():
        job = await runner.spawn('fixture', model_or_combo=leg.model, role=role,
                                 client=leg.harness, cwd=str(tmp_path), wait_seconds=3)
        await runner._tasks[job.job_id]
        return job

    job = asyncio.run(run())
    assert gw.tagged(seen['env']['OPENCODE_CONFIG_CONTENT'], job.job_id, 'omniroute')
    assert seen['expected_tag'] in seen.get('fetch_tags', [])
    assert fetch('wrong-lookup-tag', seen['env']) == []
    if row_kind in ('matching', 'prefixed', 'base-model', 'failed-run', 'different-gpt'):
        assert job.served_model == 'cx/' + served
        assert job.served_family == 'gpt'
        assert job.witness_kind == gw.WITNESS_KIND
        mismatches = [e for e in job.events if e['event'] == 'served_model_mismatch']
        if served != leg.model.removeprefix('omniroute/cx-'):
            assert mismatches == [dict(event='served_model_mismatch',
                                       requested_model=leg.model, served_model='cx/' + served)]
        else:
            assert mismatches == []
    else:
        assert job.served_model in (None, '', 'unwitnessed')
        assert job.served_family in (None, '', 'unwitnessed')
        assert any(e['event'] == 'gateway_witness_unavailable' for e in job.events)


def test_route_table_witness_paths():
    """Run with -s to list all legs without an authenticated witness path.

    Includes disabled and explicit legs so route changes cannot hide gaps.
    Gateway capability assumes the MiMo endpoint is the configured gateway.
    """
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.spawner.served_telemetry import native_models

    missing = []
    for leg in (*DEFAULT_LEGS, *EXPLICIT_LEGS):
        cfg = {'provider': {'omniroute': {}, 'mimo': {}}}
        gw.tag_session(cfg, 'coverage')
        env = {'OPENCODE_CONFIG_CONTENT': json.dumps(cfg), 'OMNIROUTE_BASE_URL': '/gateway/v1'}
        if leg.harness == 'opencode' and gw.gateway_leg(leg.model, env):
            runner = SpawnerRunner()
            job = _job(leg.model)
            runner.jobs['coverage'] = job
            runner._adapter_tokens['coverage'] = object()
            rows = [{'sessionTag': gw.session_tag('coverage'), 'status': 200, 'model': leg.model}]
            gw.witness(runner, 'coverage', job, env, fetch=lambda *a: rows)
            if job.served_family is None:
                missing.append(leg.id)
            else:
                assert job.served_family == leg.family, leg.id
        elif native_models(leg.harness, json.dumps(
                {'type': 'system', 'subtype': 'init', 'model': leg.model})):
            continue
        else:
            missing.append(leg.id)
    print('Route-table legs without a witness path (including disabled legs):\n' + '\n'.join(missing))
    assert not set(missing).intersection(leg.id for leg in _SOL_LEGS), missing


@pytest.mark.parametrize('model', ['console-meta/muse-spark-1.3', 'omniroute/resolver-x'])
def test_adapter_rejects_unregistered_model_segments(model):
    from plangraph.spawner.runner import JobInfo, SpawnerRunner
    runner = SpawnerRunner()
    job = JobInfo('admission', 'running', 'fixture', 'omniroute/cx-gpt-6.1-sol')
    runner.jobs[job.job_id] = job
    token = runner._adapter_tokens[job.job_id] = object()
    with pytest.raises(ValueError, match='not an admitted model id'):
        runner.served_witness(job.job_id, model, adapter_token=token)
    assert job.served_model == job.served_family == 'unwitnessed'


@pytest.mark.parametrize('model,family', [
    ('console-meta/muse-spark-1.3-contributor', 'muse'),
    ('omniroute/muse-spark-1.3-contributor', 'muse'),
    ('cx/gpt-6.1-sol-medium', 'gpt'),
    ('omniroute/cx-gpt-6.1-sol-medium', 'gpt'),
    ('xiaomi-mimo/mimo-v2.6-flash', 'mimo'),
])
def test_adapter_family_uses_registered_model_segment(model, family):
    from plangraph.spawner.runner import JobInfo, SpawnerRunner
    runner = SpawnerRunner()
    job = JobInfo('admission', 'running', 'fixture', model)
    runner.jobs[job.job_id] = job
    token = runner._adapter_tokens[job.job_id] = object()
    runner.served_witness(job.job_id, model, adapter_token=token)
    assert job.served_family == family
