"""The launch endpoint's listener is checked before any snapshot-error recovery, and a management
endpoint vouches only for the launch endpoint it covers (synthetic, no live access)."""
import asyncio
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state
from plangraph.spawner import leg_probe
from test_leg_adapter import isolated  # noqa: F401  (autouse: no external access)

MANAGEMENT = 'http://127.0.0.1:8080'
OPEN, CLOSED = 'http://127.0.0.1:8080/v1', 'http://127.0.0.1:8081/v1'
MIMO = SimpleNamespace(id='mimo-v2.6-flash', model='mimo/mimo-v2.6-flash', harness='opencode')


def pool(name):
    return SimpleNamespace(available=True, stale=False, reason='', until='')


def reader(tmp_path, snapshot, *, launch, listening, listed=('omniroute/mimo-v2.6-flash',)):
    seen = []

    def probe(url):
        seen.append(url)
        return url in listening

    store = leg_state.LegStateStore(tmp_path / 'state')
    provider = SimpleNamespace(snapshot=snapshot, base_url=MANAGEMENT)
    adapter = leg_state.LegAdapter(store, provider, lambda: listed, enabled=True, clock=lambda: 0,
                                   gateway_probe=probe, gateway_url=lambda leg: launch)
    return adapter, seen


def refuse():
    raise OSError('snapshot unavailable')


def test_a_snapshot_error_does_not_skip_the_launch_endpoint_check(tmp_path, monkeypatch):
    spawned = []

    async def probe(*args, **kwargs):
        spawned.append(args)
        return dict(ok=True)

    monkeypatch.setattr(leg_probe, 'probe', probe)
    adapter, seen = reader(tmp_path, refuse, launch=CLOSED, listening=())
    status = adapter(MIMO.model)
    assert (status['state'], status['reason']) == ('down', 'gateway_unreachable') and seen == [CLOSED]
    skip = asyncio.run(leg_probe.before_launch(SimpleNamespace(leg_reader=adapter), MIMO, 'review', False))
    assert skip == 'gateway_unreachable' and spawned == []


def test_a_snapshot_error_with_a_listening_endpoint_still_recovers_by_probe(tmp_path):
    adapter, _ = reader(tmp_path, refuse, launch=OPEN, listening=(OPEN,))
    assert adapter(MIMO.model)['reason'].startswith('leg_state_snapshot_error:')


def test_a_closed_launch_endpoint_is_checked_again_after_a_failed_refresh(tmp_path):
    adapter, seen = reader(tmp_path, refuse, launch=CLOSED, listening=())
    adapter(MIMO.model)
    adapter._at = None
    adapter(MIMO.model)
    assert seen == [CLOSED, CLOSED]


def test_a_healthy_management_endpoint_does_not_vouch_for_a_different_launch_endpoint(tmp_path):
    snapshot = lambda: {'deepseek': pool('deepseek')}
    model = 'omniroute/deepseek-v4.1-flash'
    adapter, seen = reader(tmp_path, snapshot, launch=CLOSED, listening=(OPEN,), listed=(model,))
    status = adapter(model)
    assert (status['state'], status['reason']) == ('down', 'gateway_unreachable') and seen == [CLOSED]


def test_the_snapshot_vouches_for_the_launch_endpoint_it_covers(tmp_path):
    snapshot = lambda: {'deepseek': pool('deepseek')}
    model = 'omniroute/deepseek-v4.1-flash'
    adapter, seen = reader(tmp_path, snapshot, launch=OPEN, listening=(), listed=(model,))
    assert adapter(model)['state'] == 'ok' and seen == []


def test_the_omniroute_launch_endpoint_comes_from_the_child_config(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_GATEWAY_URL', 'http://host-a.example:9000')
    monkeypatch.delenv('OMNIROUTE_BASE_URL', raising=False)
    assert leg_state.gateway_url('omniroute/deepseek-v4.1-flash') == 'http://host-a.example:9000/v1'
