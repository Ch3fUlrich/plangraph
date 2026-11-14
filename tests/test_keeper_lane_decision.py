"""Lane decision-only closure and contradictory tier prevention."""
import json

import pytest
from keeper_util import defn
from test_keeper_decision_close import git, world  # noqa: F401 -- shared fixture

from plangraph.keeper.errors import Refused


@pytest.fixture
def w(request):
    return request.getfixturevalue("world")


@pytest.mark.parametrize('declared', [False, True])
@pytest.mark.parametrize('kind', ['lane', 'gate', 'goal', 'question', 'loop'])
def test_spawner_agy_witness_lane(w, declared, kind):
    key = 'demo:SPAWNER-AGY-WITNESS'
    w.store.create_items(w.p('router'), [defn(
        'SPAWNER-AGY-WITNESS', kind='lane', review_tier='none',
        evidence_kinds=['decision'], done_criteria='Close on the decision-only branch'),
        defn('LANE-GOAL', ['SPAWNER-AGY-WITNESS'], kind='goal')])
    epoch = w.store.claim(w.p('workerA'), key)['epoch']
    w.store.add_evidence_router(w.p('router'), key, 'decision', {'id': 'D-5024'})
    # Reproduce the pre-existing high-tier lane; new contradictory definitions
    # are refused separately rather than preventing legacy close remediation.
    w.conn.execute('UPDATE items SET kind=?, review_tier=?, evidence_kinds=? WHERE key=?',
                       (kind, 'high', json.dumps(['decision'] if declared else ['any']), key))
    w.log.write_text(f'| D-5024 | CLOSE {key} |\n')
    git(w.repo, 'commit', '-am', 'lane decision')
    git(w.repo, 'push', 'origin', 'main')
    if declared:
        result = w.store.close_on_decision(w.p('workerA'), key,
            decision_ref='D-5024', epoch=epoch)
        assert result['state'] == 'done'
    else:
        with pytest.raises(Refused, match='evidence_kinds.*decision') as exc:
            w.store.close_on_decision(w.p('workerA'), key,
                decision_ref='D-5024', epoch=epoch)
        assert exc.value.code == 'bad_state'


@pytest.mark.parametrize('tier', ['standard', 'high', None])
def test_create_decision_only_needs_none(w, tier):
    item = defn('NEW', evidence_kinds=['decision'])
    if tier is not None:
        item['review_tier'] = tier
    with pytest.raises(Refused, match="review_tier.*none") as exc:
        w.store.create_items(w.p('router'), [item, defn('NEW-GOAL', ['NEW'], kind='goal')])
    assert exc.value.code == 'validation'
    assert not w.conn.execute("SELECT 1 FROM items WHERE key='demo:NEW'").fetchone()


@pytest.mark.parametrize('fields', [
    {'review_tier': 'high'},
    {'review_tier': 'standard'},
    {'review_tier': 'high', 'evidence_kinds': ['decision']},
])
def test_edit_cannot_leave_decision_only_review_tier(w, fields):
    with pytest.raises(Refused, match="review_tier.*none"):
        w.store.edit_definition(w.p('router'), 'demo:A', fields)
    assert w.store._get_or_refuse('demo:A')['review_tier'] == 'none'


def test_edit_evidence_alone_refused(w):
    w.store.create_items(w.p('router'), [defn('NEW', review_tier='high'),
        defn('NEW-GOAL', ['NEW'], kind='goal')])
    with pytest.raises(Refused, match="review_tier.*none"):
        w.store.edit_definition(w.p('router'), 'demo:NEW', {'evidence_kinds': ['decision']})


def test_mixed_evidence_does_not_bypass_lane_guard(w):
    w.conn.execute("UPDATE items SET review_tier='high', evidence_kinds=? WHERE key='demo:A'",
                       (json.dumps(['decision', 'commit']),))
    with pytest.raises(Refused, match='evidence_kinds.*decision'):
        w.store.close_on_decision(w.p('workerA'), 'demo:A', decision_ref='D-5024', epoch=w.epoch)
