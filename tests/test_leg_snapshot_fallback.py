"""Snapshot read failures retain the production reader's declared-state fallback."""
import asyncio
from types import SimpleNamespace

from plangraph.routing_support import leg_state, omniroute_state
from plangraph.routing_support.leg_state_flag import default_reader as production_reader
from plangraph.routing_support.liveness import require_available
from plangraph.spawner import leg_probe


def test_default_reader_snapshot_error_fallback_still_probes(tmp_path, monkeypatch, caplog):
    reads, probes = [], []

    def snapshot():
        reads.append('read')
        raise OSError('snapshot unavailable')

    store = leg_state.LegStateStore(tmp_path / 'legs.sqlite')
    monkeypatch.setattr(leg_state, '_default_store', lambda: store)
    monkeypatch.setattr(omniroute_state, 'from_env', lambda: SimpleNamespace(snapshot=snapshot))
    monkeypatch.setattr(leg_state.LegAdapter, 'standalone_models', staticmethod(lambda: []))
    monkeypatch.delenv('PLANGRAPH_OMNIROUTE_LEG_STATE', raising=False)
    production_reader.cache_clear()
    try:
        reader = production_reader()
        leg = SimpleNamespace(id='sample', model='opencode-go/sample', harness='opencode')
        require_available(leg, reader, {})
        assert reader(leg.model) == dict(state='ok', evidence=None,
            reason='leg_state_snapshot_error: OSError: snapshot unavailable')

        async def probe(*args, **kwargs):
            assert kwargs['store'] is store
            probes.append('probe')
            return dict(ok=True)

        monkeypatch.setattr(leg_probe, 'probe', probe)
        assert asyncio.run(leg_probe.before_launch(
            SimpleNamespace(leg_reader=reader), leg, 'review', False)) is None
        assert reads == ['read', 'read'] and probes == ['probe']
        assert 'leg_state_snapshot_error: OSError: snapshot unavailable' in caplog.text
    finally:
        production_reader.cache_clear()


def test_snapshot_recovery_log_redacts_error_values(tmp_path, monkeypatch, caplog):
    secret = 'snapshot-credential'
    monkeypatch.setenv('SNAPSHOT_TEST_TOKEN', secret)
    store = leg_state.LegStateStore(tmp_path / 'legs.sqlite')

    def snapshot():
        raise OSError('snapshot unavailable: ' + secret + ' Bearer sk-example')

    reader = leg_state.LegAdapter(store, SimpleNamespace(snapshot=snapshot),
                                  models=[], enabled=True)
    leg = SimpleNamespace(model='opencode-go/sample', harness='opencode')

    async def probe(*args, **kwargs):
        return dict(ok=True)

    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(
        SimpleNamespace(leg_reader=reader), leg, 'review', False)) is None
    assert 'leg_state_snapshot_error: OSError: snapshot unavailable' in caplog.text
    assert secret not in caplog.text and 'sk-example' not in caplog.text
    assert '[autoos:redacted]' in caplog.text and '[REDACTED]' in caplog.text
