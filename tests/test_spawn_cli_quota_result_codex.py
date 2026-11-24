"""A native Codex writer's usage-limit stop is booked in the leg state the route reads."""
import time

from plangraph.planner.legs import REVIEW_GPT, LEAD_WRITER, review_route_pool
from plangraph.planner.routing import plan_spawn
from plangraph.spawner.cli import main
from plangraph.spawner.leg_probe import DispatchReader
from plangraph.spawner.leg_state import LegStateStore


def _book(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    reset = time.time() + 3600
    assert main(['--state', str(tmp_path), 'quota-result', '--model', 'gpt-6.2-sol',
                 '--harness', 'codex', '--error', 'Error: usage limit reached.',
                 '--reset-at', str(reset)]) == 0
    return LegStateStore(tmp_path / 'plangraph' / 'leg-state.sqlite')


def test_quota_result_limits_every_routed_codex_leg_and_review_seat(tmp_path, monkeypatch):
    store = _book(tmp_path, monkeypatch)
    codex = [leg for leg in review_route_pool() if leg.harness == 'codex']
    assert REVIEW_GPT in {leg.id for leg in codex}
    for leg in codex:
        assert store.leg_status(leg.model, harness='codex')['state'] == 'limited', leg.id
    assert store.leg_status(REVIEW_GPT, harness='codex')['state'] == 'limited'
    assert store.leg_status('gpt-6.2-sol', harness='codex')['state'] == 'limited'
    assert store.leg_status('gpt-6.2-sol-other', harness='codex')['state'] == 'ok'


def test_route_leaves_the_codex_lead_writer_for_another_writer(tmp_path, monkeypatch):
    lead = plan_spawn(dict(estimate_tokens=1000), role='implement', catalog_rows=[],
                      leg_reader=LegStateStore(tmp_path / 'plangraph' / 'none.sqlite'))
    assert lead.first_leg.id == LEAD_WRITER, lead.to_dict()
    store = _book(tmp_path, monkeypatch)
    plan = plan_spawn(dict(estimate_tokens=1000), role='implement', catalog_rows=[],
                      leg_reader=DispatchReader(store, None))
    assert plan.first_leg is not None, plan.to_dict()
    assert plan.first_leg.harness != 'codex', plan.to_dict()
    assert not any(leg.harness == 'codex' for leg in plan.legs), plan.to_dict()
