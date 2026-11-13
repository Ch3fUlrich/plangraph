"""Writer choice stays binding across unavailable legs."""
import pytest
from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.routing_support.leg_state_flag import service_plan
from types import SimpleNamespace
from plangraph.planner.route_policy import RoutePolicy


@pytest.mark.parametrize('allow,expected', [(False, []), (True, ['gemini'])])
def test_down_writer_family_never_silently_changes(tmp_path, monkeypatch, allow, expected):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    monkeypatch.setenv('OMNIROUTE_BASE_URL', 'https://gateway.example.invalid/v1')
    monkeypatch.setenv('AUTOOS_OMNIROUTE_KEY', 'fixture-key')
    legs = [l for l in DEFAULT_LEGS if l.id in ('cx-gpt-6.1-sol-xhigh', 'agy-gemini-3.8-flash')]
    def state(model, **kw):
        return {'state': 'down' if 'sol' in model else 'ok', 'evidence': None}
    card = {'estimate_tokens': 100, 'writer_family': 'openai', 'allow_family_fallthrough': allow}
    plan = service_plan(SimpleNamespace(leg_reader=state), card=card, role='implement', legs=legs, catalog_rows=[],
                      policy=RoutePolicy())
    assert [leg.family for leg in plan.legs] == expected
    if not allow:
        assert 'writer_family_unavailable' in str(plan.error)


def test_failed_launch_still_names_actual_leg(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from plangraph.spawner.seat_spawn import SeatSpawnMixin
    chosen = next(l for l in DEFAULT_LEGS if l.id == 'agy-gemini-3.8-flash')
    job = SimpleNamespace(job_id='fake', status='failed', exit_code=1, events=[])
    async def spawn(**kw):
        return job
    service = SeatSpawnMixin()
    service.runner = SimpleNamespace(spawn=spawn, _tasks={}, _processes={})
    service._running_records = {}
    service._save = lambda r: None
    service._path = lambda _: tmp_path / 'record.json'
    record = {'role': 'implement', 'run_id': 'run-fake'}
    asyncio.run(service._launch_leg(record, chosen, prompt_record=None))
    assert record['launched_leg']['id'] == chosen.id
