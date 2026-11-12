"""Only D-5240 production writers fall back; disabled writers are refused."""
import asyncio

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, WRITER_ORDER
from plangraph.planner.routing import SpawnRefused, plan_spawn
from plangraph.spawner.service import SpawnService


@pytest.mark.parametrize('leg', [l for l in DEFAULT_LEGS
                               if l.id not in WRITER_ORDER and l.disabled is None])
def test_disabled_writer_refusal(tmp_path, leg):
    with pytest.raises(SpawnRefused, match=r'writer disabled \(D-3828\)'):
        asyncio.run(SpawnService(state=tmp_path).spawn(
            card={'estimate_tokens': 100}, leg=leg.id,
            override_reason='writer policy regression', dry_run=True))


def test_sol_only_and_no_fallback():
    # D-5240 adds the subscription Gemini writer ahead of paid Muse.
    assert WRITER_ORDER == ('cx-gpt-6.1-sol', 'agy-gemini-3.8-flash', 'opencode-console-muse-spark')
    plan = plan_spawn({'estimate_tokens': 100}, role='implement')
    assert [l.id for l in plan.legs] == list(WRITER_ORDER)
    sol_out = plan_spawn({'estimate_tokens': 100}, role='implement', leg_reader=lambda model, **kw: (
        {'state': 'limited' if model == 'omniroute/cx-gpt-6.1-sol-xhigh' else 'available',
         'evidence': {'status': 429} if model == 'omniroute/cx-gpt-6.1-sol-xhigh' else None}))
    assert [l.id for l in sol_out.legs] == ['agy-gemini-3.8-flash', 'opencode-console-muse-spark']
    limited = plan_spawn({'estimate_tokens': 100}, role='implement',
                         leg_reader=lambda *a, **kw: {'state': 'limited', 'evidence': {'status': 429}})
    assert not limited.legs
