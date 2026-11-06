import asyncio
from dataclasses import replace

import pytest

from plangraph.planner import legs
from plangraph.planner.routing import SpawnRefused
from plangraph.spawner.service import SpawnService


@pytest.mark.parametrize('harness', ['opencode', 'agy'])
@pytest.mark.parametrize('pin', ['leg', 'model'])
@pytest.mark.parametrize('free_up,reason', [(True, None), (False, None),
    (True, 'D-4382: no free leg witnesses for this task')])
def test_paid_pin(tmp_path, monkeypatch, harness, pin, free_up, reason):
    paid = replace(legs.DEFAULT_LEGS[0], id='paid-test', model='test/paid',
                   tier='paid', harness=harness, disabled=None,
                   roles=('review',), family='paid-family', tool_mode='tools')
    free = replace(paid, id='free-test', model='test/free', tier='free', family='free-family')
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (paid, free))
    service = SpawnService(state=tmp_path, leg_reader=lambda model, **kw:
        {'state': 'available' if model != free.model or free_up else 'down', 'evidence': None})
    options = dict(card={'estimate_tokens': 100}, role='review', writer_families=['gpt'],
                   override_reason=reason, dry_run=True,
                   **{pin: paid.id if pin == 'leg' else paid.model})
    if free_up and reason is None:
        with pytest.raises(SpawnRefused, match='free-test'):
            asyncio.run(service.spawn(**options))
        assert not (tmp_path / 'spawn-overrides.jsonl').exists()
    else:
        # Pin auditing still requires a reason even if no free leg serves.
        options['override_reason'] = reason or 'No eligible free leg is up'
        record = asyncio.run(service.spawn(**options))
        assert record['status'] == 'dry_run'
        assert service.result(record['run_id'])['override_reason'] == options['override_reason']


def test_only_sol_writes():
    assert legs.WRITER_ORDER == ('cx-gpt-6.1-sol',)
    assert [leg.id for leg in legs.DEFAULT_LEGS if 'implement' in leg.roles] == list(legs.WRITER_ORDER)
    assert {'deepseek', 'openrouter-free', 'mimo-v2.6-pro'} <= {leg.id for leg in legs.DEFAULT_LEGS}
