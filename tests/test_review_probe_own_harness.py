"""Scheduled review probe: every routed review leg is spawned through its own harness
(LE-REVIEW-PROBE-SONNET-SPAWNED-THROUGH-AGY-CLIENT)."""
import asyncio
from types import SimpleNamespace

from plangraph.routing_support.leg_state import LegStateStore


def test_every_routed_review_leg_is_spawned_through_its_own_harness(tmp_path, monkeypatch):
    from plangraph.planner import legs
    from plangraph.routing_errors import SpawnRefused
    from plangraph.spawner import leg_probe
    from plangraph.spawner.target import resolve_launch_target

    seen = {}

    async def spawn(**kwargs):
        # The head of SpawnerRunner.spawn: target resolution and the client check.
        _, resolved = resolve_launch_target(kwargs['model_or_combo'], chosen=kwargs.get('chosen'),
                                            client=kwargs.get('client'))
        if resolved is not None and kwargs.get('client') != resolved.harness:
            raise SpawnRefused('client disagrees with leg harness: ' + resolved.id)
        seen[resolved.id] = resolved.harness
        return SimpleNamespace(job_id='j', status='completed', exit_code=0, stdout='OK', stderr='')

    async def cancel(job_id):
        pass

    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *args, **kwargs: '/bin/x')
    monkeypatch.setattr(leg_probe, '_check_manage_key', lambda runner: None)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = SimpleNamespace(leg_reader=store, spawn=spawn, cancel=cancel)
    routed = legs.routed_review_legs()
    reply = asyncio.run(leg_probe.probe_many(runner, all_reviewers=True, timeout=5))
    refused = [row for row in reply['probes'] if 'SpawnRefused' in row.get('reason', '')]
    assert not refused, refused
    assert seen == {leg.id: leg.harness for leg in routed}
    assert seen[legs.REVIEW_SONNET] == 'claude'
