"""Dynamic model admission (D-3259)."""
import pytest
from plangraph.planner.routing import SpawnRefused


def test_gateway_admission():
    from plangraph.planner.model_admission import validate_model
    assert validate_model('omniroute/fresh-model', gateway_list=lambda: {'fresh-model'},
                          opencode_list=lambda: set()) == 'omniroute/fresh-model'


def test_unlisted():
    from plangraph.planner.model_admission import validate_model
    with pytest.raises(SpawnRefused, match='not in catalog, gateway list or opencode models'):
        validate_model('fixture/absent', gateway_list=lambda: set(), opencode_list=lambda: set())


def test_host_admission():
    from plangraph.planner.model_admission import validate_model
    model = 'console-anthropic/claude-future'
    assert validate_model(model, opencode_list=lambda: {model}) == model


def test_cache_ttl_and_unavailable(tmp_path, monkeypatch):
    from plangraph.planner.model_admission import cached_models
    monkeypatch.setenv('PLANGRAPH_MODEL_LIST_TTL_S', '10')
    calls = []
    def loader():
        calls.append(1)
        return {'fresh'}
    assert cached_models('test', loader, state=tmp_path, clock=lambda: 100) == {'fresh'}
    assert cached_models('test', loader, state=tmp_path, clock=lambda: 109) == {'fresh'}
    assert len(calls) == 1
    cached_models('test', loader, state=tmp_path, clock=lambda: 110)
    assert len(calls) == 2
    def fail():
        raise OSError('unreachable')
    assert cached_models('bad', fail, state=tmp_path, clock=lambda: 100) == set()


def test_removed_never_looks_up():
    from plangraph.planner.model_admission import validate_model
    from plangraph.routing_support.resolver import OPERATOR_REMOVED_MODELS
    def forbidden():
        pytest.fail('removed model must not trigger lookup')
    for model in OPERATOR_REMOVED_MODELS:
        with pytest.raises(SpawnRefused):
            validate_model(model, gateway_list=forbidden, opencode_list=forbidden)


def test_family_detection():
    from plangraph.routing_support.resolver import detect_model_family
    assert detect_model_family('console-anthropic/claude-sonnet-5-5') == 'claude'
    assert detect_model_family('omniroute/claude-future') == 'claude'


def test_mimo_policy_before_lookup():
    from plangraph.planner.model_admission import validate_model
    from plangraph.mimo_policy import MimoPathError
    def forbidden():
        pytest.fail('MiMo violation must not trigger lookup')
    with pytest.raises(MimoPathError):
        validate_model('omniroute/mimo-v2.6-pro', gateway_list=forbidden, opencode_list=forbidden)
