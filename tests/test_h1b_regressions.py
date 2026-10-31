"""H1b seat findings (D-3258/D-3259/D-3264, fixer round 3).

Every test here fails on the pre-fix tree:

* LOW  host_models: spawn state dirs are created 0o700 on first write.
* HIGH runtime denial expires (TTL) and is matched on error-shaped lines
       only - pytest prose carrying a marker is never harness evidence.

The --state, providerless-gate, listing-match and loader-backoff findings
moved to plangraph:SPAWNER-CONSOLE-HOST-STATE-FU (D-3295).
"""
import stat
from pathlib import Path
from types import SimpleNamespace

from plangraph.routing_support.resolver import host_skip_reason

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
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    monkeypatch.setattr(host_models, 'time', SimpleNamespace(time=lambda: 1000.0))
    host_models.mark_unavailable(MODEL, state=tmp_path)
    row = {'harness': 'opencode', 'model': MODEL}
    assert 'not_served_on_host' in host_skip_reason(row, probe=lambda e: {MODEL})
    monkeypatch.setattr(host_models, 'time', SimpleNamespace(time=lambda: 1031.0))
    assert host_models.unavailable_models(state=tmp_path) == set()
    assert host_skip_reason(row, probe=lambda e: {MODEL}) is None


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


def test_runtime_denial_reset_is_documented():
    """HIGH: the operator-visible reset path is written down (D-3258)."""
    docs = (Path(__file__).resolve().parents[1] / 'docs/SPAWNER.md').read_text(encoding='utf-8')
    assert 'host-unavailable' in docs
    assert 'expire' in docs.lower()
