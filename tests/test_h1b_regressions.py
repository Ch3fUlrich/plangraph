"""H1b seat findings (D-3258/D-3259/D-3264, fixer round 3).

Every test here fails on the pre-fix tree:

* LOW  host_models: spawn state dirs are created 0o700 on first write.
* LOW  model_admission.console_host_skip: the affirmative-evidence lookup
       honours the pinned ``--state`` directory.
* LOW  resolver.host_skip_reason: a providerless model runtime denials marked
       is gated ``not_served_on_host`` on later routes.
* HIGH runtime denial expires (TTL) and is matched on error-shaped lines
       only - pytest prose carrying a marker is never harness evidence.
* MED  denial write dir == read dir: ``--state`` is honoured everywhere.
* MED  host listing match is case-insensitive and #variant-aware like the
       gateway path.
* MED  a failing listing loader backs off instead of re-running the 20 s
       ``opencode models`` subprocess once per leg.
"""
import json
import stat
import time
from pathlib import Path
from types import SimpleNamespace

from plangraph.routing_support.resolver import host_skip_reason, plan_for_launch

MODEL = 'console-anthropic/fixture-opus-4-8'


def test_state_dir_is_private_from_first_write(tmp_path):
    """LOW: the denial file and listing cache land in a 0o700 directory."""
    from plangraph.planner.host_models import mark_unavailable
    state = tmp_path / 'fresh'
    mark_unavailable(MODEL, state=state)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_runtime_denial_expires(tmp_path, monkeypatch):
    """HIGH: a runtime denial is a correction for now, not forever."""
    from plangraph.planner import host_models
    monkeypatch.setenv('PLANGRAPH_DENIED_TTL_S', '30')
    monkeypatch.setattr(host_models, 'time', SimpleNamespace(time=lambda: 1000.0))
    host_models.mark_unavailable(MODEL, state=tmp_path)
    row = {'harness': 'opencode', 'model': MODEL}
    assert 'not_served_on_host' in host_skip_reason(row, probe=lambda e: {MODEL}, state=tmp_path)
    monkeypatch.setattr(host_models, 'time', SimpleNamespace(time=lambda: 1031.0))
    assert host_models.unavailable_models(state=tmp_path) == set()
    assert host_skip_reason(row, probe=lambda e: {MODEL}, state=tmp_path) is None


def test_prose_markers_are_not_runtime_denial_evidence():
    """HIGH: pytest prose carrying a marker must not blacklist a model."""
    from plangraph.spawner.env_redact import unavailable_reason
    prose = [
        "E       assert 'not_served_on_host' in result['error']",
        "E   SpawnRefused: unknown model (not in catalog, gateway list or "
        "opencode models): fixture/absent",
        "assert not_served_on_host in host_skip_reason({'harness': 'opencode'})",
        'unknown model was mentioned in the diff',
    ]
    for line in prose:
        assert unavailable_reason(1, '', line) is None, line
        assert unavailable_reason(1, line, '') is None, line
    # Error-shaped lines remain runtime evidence.
    assert unavailable_reason(1, '', 'Model unavailable: ' + MODEL)
    assert unavailable_reason(1, '', 'prefix not_served_on_host')


def test_denial_write_dir_equals_read_dir(tmp_path, monkeypatch):
    """MED: a denial is read where it was written (``--state`` wins over env)."""
    from plangraph.planner.host_models import mark_unavailable
    write_dir, elsewhere = tmp_path / 'work', tmp_path / 'elsewhere'
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(elsewhere))
    mark_unavailable(MODEL, state=write_dir)
    row = {'harness': 'opencode', 'model': MODEL}
    assert 'not_served_on_host' in host_skip_reason(row, probe=lambda e: {MODEL}, state=write_dir)
    plan = plan_for_launch(_plan(MODEL), probe=lambda e: {MODEL}, state=write_dir)
    assert [s['skip_reason'] for s in plan.skipped] == ['not_served_on_host: ' + MODEL]
    # Nothing wrote the env dir: it must not gate.
    assert host_skip_reason(row, probe=lambda e: {MODEL}) is None


def _plan(model):
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    return SpawnPlan((replace(DEFAULT_LEGS[0], id='h1b', model=model, harness='opencode'),),
                     (), 'test', (), ())


def test_host_listing_match_is_case_insensitive_and_variant_aware(tmp_path, monkeypatch):
    """MED: the listing matcher behaves like the gateway path (D-3264)."""
    from plangraph.planner import model_admission
    calls = []
    monkeypatch.setattr(model_admission, 'opencode_models',
                        lambda: calls.append(1) or set())
    _listing(tmp_path, {'Console-Anthropic/FIXTURE-OPUS-4-8#default'})
    assert host_skip_reason({'harness': 'opencode', 'model': MODEL}, state=tmp_path) is None
    assert not calls
    assert 'not_served_on_host' in host_skip_reason(
        {'harness': 'opencode', 'model': MODEL + '#other'}, state=tmp_path)
    assert model_admission.validate_model(
        MODEL, opencode_list=lambda: {'console-anthropic/fixture-opus-4-8#default'}) == MODEL


def test_failed_loader_backs_off_instead_of_rerunning_per_leg(tmp_path):
    """MED: format drift probes once per backoff window, not once per leg."""
    from plangraph.planner.model_admission import cached_models
    calls = []
    now = [0.0]

    def fail():
        calls.append(1)
        raise OSError('opencode models output unrecognised')

    for _ in range(12):
        assert cached_models('opencode', fail, state=tmp_path, clock=lambda: now[0]) == set()
    assert len(calls) == 1, 'a failed loader must not re-run per leg'
    now[0] = 31.0
    assert cached_models('opencode', fail, state=tmp_path, clock=lambda: now[0]) == set()
    assert len(calls) == 2
    assert not (tmp_path / 'model-list-opencode.json').exists()


def test_runtime_denial_reset_is_documented():
    """HIGH: the operator-visible reset path is written down (D-3258)."""
    docs = (Path(__file__).resolve().parents[1] / 'docs/SPAWNER.md').read_text(encoding='utf-8')
    assert 'host-unavailable' in docs
    assert 'expire' in docs.lower()
