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
    assert not (tmp_path / 'model-list-bad.json').exists()


def test_unreachable_is_not_unknown(tmp_path):
    from plangraph.planner.model_admission import validate_model
    def fail():
        raise OSError('offline')
    with pytest.raises(SpawnRefused, match='unreachable'):
        validate_model('omniroute/new', gateway_list=fail, opencode_list=lambda: set())


def test_providerless_host_model():
    from plangraph.planner.model_admission import validate_model
    assert validate_model('future-local', opencode_list=lambda: {'future-local'}) == 'future-local'


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


def test_parse_opencode_models_plain():
    from plangraph.planner.opencode_listing import parse_opencode_models
    text = 'anthropic/claude-sonnet-4-5\nopenai/gpt-5-codex\n'
    assert parse_opencode_models(text) == {'anthropic/claude-sonnet-4-5', 'openai/gpt-5-codex'}


def test_parse_opencode_models_annotated():
    from plangraph.planner.opencode_listing import parse_opencode_models
    text = 'anthropic/claude-sonnet-4-5 (default)\nopenai/gpt-5-codex  400k context  released 2026-01-01\n'
    assert parse_opencode_models(text) == {'anthropic/claude-sonnet-4-5', 'openai/gpt-5-codex'}


def test_parse_opencode_models_columnar():
    from plangraph.planner.opencode_listing import parse_opencode_models
    text = ('MODEL                        PROVIDER   CONTEXT\n'
            'anthropic/claude-sonnet-4-5  anthropic  200000\n'
            'openai/gpt-5-codex           openai     400000\n')
    assert parse_opencode_models(text) == {'anthropic/claude-sonnet-4-5', 'openai/gpt-5-codex'}


def test_parse_opencode_models_non_ascii_bytes():
    from plangraph.planner.opencode_listing import parse_opencode_models
    raw = ('anthropic/claude-sonnet-4-5  默认 (défaut)\n'
           'openai/gpt-5-codex  café ☕ 2026\n').encode('utf-8')
    assert parse_opencode_models(raw) == {'anthropic/claude-sonnet-4-5', 'openai/gpt-5-codex'}


def test_parse_opencode_models_providerless_and_empty():
    from plangraph.planner.opencode_listing import parse_opencode_models
    assert parse_opencode_models('future-local\n') == {'future-local'}
    assert parse_opencode_models('') == set()


def test_opencode_models_wires_parser_and_utf8(tmp_path, monkeypatch):
    from plangraph.planner import model_admission
    import subprocess
    seen = {}
    class Result:
        returncode = 0
        stdout = 'anthropic/claude-sonnet-4-5  (default)\nMODEL  PROVIDER\nopenai/gpt-5-codex  openai\n'
    def fake(run, **kwargs):
        seen.update(kwargs)
        return Result()
    monkeypatch.setattr(subprocess, 'run', fake)
    monkeypatch.setattr(model_admission.shutil, 'which', lambda name: '/bin/opencode')
    assert model_admission.opencode_models() == {'anthropic/claude-sonnet-4-5', 'openai/gpt-5-codex'}
    assert seen.get('encoding') == 'utf-8'


def test_opencode_models_unrecognised_output_is_uncached(tmp_path, monkeypatch):
    from plangraph.planner import model_admission
    import subprocess
    class Result:
        returncode = 0
        stdout = 'display names only, no ids here\n'
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: Result())
    monkeypatch.setattr(model_admission.shutil, 'which', lambda name: '/bin/opencode')
    with pytest.raises(OSError, match='unrecognised'):
        model_admission.opencode_models()
    monkeypatch.setenv('PLANGRAPH_MODEL_LIST_TTL_S', '600')
    assert model_admission.cached_models('opencode', model_admission.opencode_models,
                                         state=tmp_path) == set()
    assert not (tmp_path / 'model-list-opencode.json').exists()


def test_explicit_empty_lists_never_probe(monkeypatch):
    from plangraph.planner.model_admission import validate_model
    def forbidden(*args, **kwargs):
        pytest.fail('an explicit empty list must not trigger a live lookup')
    monkeypatch.setattr('plangraph.planner.model_admission.opencode_models', forbidden)
    monkeypatch.setattr('plangraph.planner.model_admission.gateway_models', forbidden)
    with pytest.raises(SpawnRefused, match='unknown model'):
        validate_model('omniroute/absent', gateway_list=[], opencode_list=[])
