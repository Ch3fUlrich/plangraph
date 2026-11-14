"""Plain-shell gateway witnesses use file configuration and retain real reasons."""
import io
import json

import pytest

from plangraph.routing_support import clients
from plangraph.spawner import gateway_witness as gw
from plangraph.spawner.runner import JobInfo
from plangraph.spawner.served_telemetry import record_native


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    for key in (*gw.URL_ENVS, gw.TOKEN_ENV, gw.TOKEN_FILE_ENV, 'OPENCODE_CONFIG_CONTENT'):
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / 'config/plangraph'
    config.mkdir(parents=True)
    return config


def child_config(provider, endpoint=None):
    options = {} if endpoint is None else {'baseURL': endpoint}
    cfg = {'provider': {provider: {'options': options}}}
    gw.tag_session(cfg, 'job')
    return {'OPENCODE_CONFIG_CONTENT': json.dumps(cfg)}


def job(provider):
    return JobInfo('job', 'completed', 'fixture prompt', provider + '/mimo-v2.6-pro')


def test_file_only_url_is_shared_by_child_and_witness(isolated_config, monkeypatch):
    endpoint = 'https://gateway.invalid/v1'
    (isolated_config / 'omniroute-base-url').write_text(endpoint + '\n')
    key = isolated_config / 'omniroute-mgmt.key'
    key.write_text('fixture-management-key\n')
    key.chmod(0o600)
    monkeypatch.setenv(gw.TOKEN_FILE_ENV, str(key))
    env = clients.build_child_env(child_config('mimo'))
    assert clients.effective_gateway_base_url() == endpoint
    assert gw._gateway(env) == endpoint
    assert gw.TOKEN_ENV not in env and gw.TOKEN_FILE_ENV not in env
    witness = job('mimo')
    seen = []
    class Runner:
        def __init__(self):
            self._adapter_tokens = {'job': object()}
        def served_witness(self, job_id, model, *, adapter_token):
            assert adapter_token is self._adapter_tokens[job_id]
            witness.served_model = model
    def opener(request, timeout):
        seen.append(True)
        assert request.full_url.startswith('https://gateway.invalid/api/usage/call-logs?')
        return io.BytesIO(json.dumps([{'sessionTag': 'plangraph-job', 'status': 200,
                                      'model': 'mimo-v2.6-pro', 'provider': 'mimo'}]).encode())
    gw.witness(Runner(), 'job', witness, env, delay=0,
               fetch=lambda tag, child: gw.fetch_rows(tag, child, opener=opener))
    record_native(witness, 'opencode', set(), witness.model_or_combo)
    assert seen == [True]
    assert witness.served_model == 'mimo/mimo-v2.6-pro'
    assert witness.witness_kind == gw.WITNESS_KIND
    assert witness.served_witness_reason == 'gateway call log (sessionTag) response-side model'


@pytest.mark.parametrize('provider', ['mimo', 'omniroute'])
def test_missing_gateway_reason_survives_native_telemetry(provider):
    witness = job(provider)
    gw.witness(None, 'job', witness, child_config(provider), attempts=1, delay=0)
    record_native(witness, 'opencode', set(), witness.model_or_combo)
    assert witness.to_dict()['served_witness_reason'] == 'gateway endpoint not configured'


def test_direct_mimo_reason_survives_native_telemetry(monkeypatch):
    monkeypatch.setenv('OMNIROUTE_BASE_URL', 'https://gateway.invalid/v1')
    witness = job('mimo')
    env = {**child_config('mimo', 'https://direct.invalid/v1'),
           'OMNIROUTE_BASE_URL': 'https://gateway.invalid/v1'}
    gw.witness(None, 'job', witness, env, fetch=lambda *_: pytest.fail('direct endpoint fetched'))
    record_native(witness, 'opencode', set(), witness.model_or_combo)
    assert witness.to_dict()['served_witness_reason'] == 'explicit direct mimo endpoint'


def test_absent_row_has_specific_reason():
    witness = job('omniroute')
    gw.witness(None, 'job', witness, child_config('omniroute'), attempts=1, fetch=lambda *_: [])
    record_native(witness, 'opencode', set(), witness.model_or_combo)
    assert witness.to_dict()['served_witness_reason'] == 'no call-log row for the session tag'


def test_nongateway_leg_keeps_native_reason():
    witness = job('direct')
    gw.witness(None, 'job', witness, child_config('direct'), fetch=lambda *_: pytest.fail('native fetched'))
    record_native(witness, 'opencode', set(), witness.model_or_combo)
    assert witness.served_witness_reason == 'opencode: emits no verified structured served-model telemetry'


def test_environment_overrides_file(isolated_config, monkeypatch):
    (isolated_config / 'omniroute-base-url').write_text('https://file.invalid/v1\n')
    monkeypatch.setenv('PLANGRAPH_GATEWAY_URL', 'https://environment.invalid/v1')
    assert gw._gateway(dict(__import__('os').environ)) == clients.effective_gateway_base_url()
    assert clients.effective_gateway_base_url() == 'https://environment.invalid/v1'
