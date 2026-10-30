from plangraph.routing_support.resolver import host_skip_reason


def test_non_gateway_requires_affirmative_listing():
    row = {'harness': 'opencode', 'model': 'local-provider/model'}
    assert 'not_served_on_host' in host_skip_reason(row, probe=lambda env: None)
    assert 'not_served_on_host' in host_skip_reason(row, probe=lambda env: set())
    assert host_skip_reason(row, probe=lambda env: {row['model']}) is None


def test_runtime_negative_overrides_listing(tmp_path, monkeypatch):
    from plangraph.planner.host_models import mark_unavailable
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    model = 'local-provider/runtime-model'
    mark_unavailable(model, state=tmp_path)
    assert 'not_served_on_host' in host_skip_reason(
        {'harness': 'opencode', 'model': model}, probe=lambda env: {model})


def test_listing_cache_host_state_and_probe_failure(tmp_path, monkeypatch):
    from plangraph.planner import host_models
    from plangraph.planner.model_admission import cached_models
    calls = []
    def load():
        calls.append(1)
        return {'provider/model'}
    monkeypatch.setattr(host_models, 'host_id', lambda: 'host-a')
    assert cached_models('opencode', load, state=tmp_path) == {'provider/model'}
    cached_models('opencode', load, state=tmp_path)
    assert len(calls) == 1
    monkeypatch.setattr(host_models, 'host_id', lambda: 'host-b')
    cached_models('opencode', load, state=tmp_path)
    cached_models('opencode', load, state=tmp_path / 'other')
    assert len(calls) == 3
    def fail():
        raise OSError('offline')
    state = tmp_path / 'failed'
    assert cached_models('opencode', fail, state=state) == set()
    assert not (state / 'model-list-opencode.json').exists()
    assert cached_models('opencode', load, state=state) == {'provider/model'}


def test_probe_exception_is_not_admitted():
    def fail(env):
        raise OSError('offline')
    assert 'not_served_on_host' in host_skip_reason(
        {'harness': 'opencode', 'model': 'another/model'}, probe=fail)
