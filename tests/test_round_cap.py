import asyncio
import json

import pytest

from plangraph.spawner.round_cap import RoundCapRefused, check_round_cap
from plangraph.spawner.service import SpawnService

ITEM = 'plangraph:ROUND-CAP-X'


def _seed(state, n, item=ITEM, pr='7', **extra):
    for i in range(n):
        d = state / 'runs' / f'run-{item.split(":")[1].lower().replace("-", "")}{pr}{i}'
        d.mkdir(parents=True)
        (d / 'record.json').write_text(json.dumps(dict(
            dict(run_id=d.name, role='implement', item_key=item, round_ref=pr, status='completed', started=i),
            **extra)))


def _svc(tmp_path):
    return SpawnService(object(), state=tmp_path)


def _card(item=ITEM, pr='7'):
    return dict(item_key=item, pr=pr, findings=['f'])


@pytest.mark.parametrize('prior', [0, 1, 2])
def test_rounds_one_to_three_allowed(tmp_path, prior):
    _seed(tmp_path, prior)
    check_round_cap(_svc(tmp_path), _card(), 'implement')


def test_fourth_round_refused_with_replan_payload(tmp_path):
    _seed(tmp_path, 3)
    with pytest.raises(RoundCapRefused) as err:
        check_round_cap(_svc(tmp_path), _card(), 'implement')
    body = json.loads(str(err.value))
    plan = body['replan_required']
    assert body['reason'] == 'round_cap'
    assert (plan['item'], plan['pr'], plan['rounds']) == (ITEM, '7', 3)
    assert plan['findings_source'] == 'card.findings'
    assert plan['replan_leg_order'] and plan['replan_card']['role'] == 'research'


def test_spawn_refuses_before_any_run(tmp_path):
    _seed(tmp_path, 3)
    with pytest.raises(RoundCapRefused):
        asyncio.run(_svc(tmp_path)._spawn(card=_card(), prompt='x'))
    assert len(list((tmp_path / 'runs').glob('run-*'))) == 3


def test_other_item_pr_review_refused_and_dry_runs_unaffected(tmp_path):
    _seed(tmp_path, 3)
    _seed(tmp_path, 2, item='plangraph:OTHER', pr='9')
    svc = _svc(tmp_path)
    check_round_cap(svc, _card('plangraph:OTHER', '9'), 'implement')
    check_round_cap(svc, _card(pr='8'), 'implement')      # other PR of the same item
    check_round_cap(svc, _card(), 'review')                # only writers are capped
    _seed(tmp_path, 1, item='plangraph:DRY', pr='1', status='dry_run')
    _seed(tmp_path, 3, item='plangraph:REF', pr='1', status='refused')
    check_round_cap(svc, _card('plangraph:REF', '1'), 'implement')
