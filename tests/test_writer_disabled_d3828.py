"""Production writers never fall back; explicit disabled writers are refused."""
import asyncio

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, WRITER_ORDER
from plangraph.planner.routing import SpawnRefused, plan_spawn
from plangraph.spawner.service import SpawnService


@pytest.mark.parametrize('leg', [l for l in DEFAULT_LEGS
                               if l.id != 'cx-gpt-6.1-sol' and l.disabled is None])
def test_disabled_writer_refusal(tmp_path, leg):
    with pytest.raises(SpawnRefused, match=r'writer disabled \(D-3828\)'):
        asyncio.run(SpawnService(state=tmp_path).spawn(
            card={'estimate_tokens': 100}, leg=leg.id,
            override_reason='writer policy regression', dry_run=True))


def test_sol_only_and_no_fallback():
    assert WRITER_ORDER == ('cx-gpt-6.1-sol',)
    plan = plan_spawn({'estimate_tokens': 100}, role='implement')
    assert [l.id for l in plan.legs] == ['cx-gpt-6.1-sol']
    limited = plan_spawn({'estimate_tokens': 100}, role='implement',
                         leg_reader=lambda *a, **kw: {'state': 'limited', 'evidence': {'status': 429}})
    assert not limited.legs
