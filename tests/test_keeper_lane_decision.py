"""Lane decision-only closure and contradictory tier prevention."""
import json

import pytest
from keeper_util import defn
from test_keeper_decision_close import world  # noqa: F401 -- shared fixture

from plangraph.keeper.errors import Refused


@pytest.fixture
def w(request):
    return request.getfixturevalue("world")


@pytest.mark.parametrize('tier', ['none', 'standard', 'high'])
@pytest.mark.parametrize('evidence', [['decision'], ['any'], ['decision', 'commit']])
@pytest.mark.parametrize('kind', ['lane', 'gate', 'goal', 'question', 'loop', 'decision', 'operator_task'])
def test_decision_close_requires_exact_evidence_and_none(w, tier, evidence, kind):
    # Stored rows must never gain a kind-based or reviewed-tier exemption.
    w.conn.execute('UPDATE items SET kind=?, review_tier=?, evidence_kinds=? WHERE key=?',
                   (kind, tier, json.dumps(evidence), 'demo:A'))
    if tier == 'none' and evidence == ['decision']:
        result = w.store.close_on_decision(w.p('router'), 'demo:A', decision_ref='D-5024')
        assert result['state'] == 'done'
    else:
        with pytest.raises(Refused, match='evidence_kinds.*decision') as exc:
            w.store.close_on_decision(w.p('router'), 'demo:A', decision_ref='D-5024')
        assert exc.value.code == 'bad_state'
        assert w.store._get_or_refuse('demo:A')['state'] == 'claimed'


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


@pytest.mark.parametrize('who', ['l1main', 'workerA', 'reviewer', 'reader', 'system'])
def test_non_router_cannot_create_decision_only_item(w, who):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p(who), [
            defn('NEW', review_tier='none', evidence_kinds=['decision']),
            defn('NEW-GOAL', ['NEW'], kind='goal')])
    assert exc.value.code == 'forbidden'
    assert not w.conn.execute("SELECT 1 FROM items WHERE key='demo:NEW'").fetchone()


@pytest.mark.parametrize('who', ['l1main', 'workerA', 'reviewer', 'reader', 'system'])
@pytest.mark.parametrize('include_tier', [False, True])
def test_non_router_cannot_declare_decision_only_on_open_none_item(w, who, include_tier):
    w.store.create_items(w.p('router'), [
        defn('NEW', review_tier='none', evidence_kinds=['decision', 'commit']),
        defn('NEW-GOAL', ['NEW'], kind='goal')])
    fields = {'evidence_kinds': ['decision']}
    if include_tier:
        fields['review_tier'] = 'none'
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p(who), 'demo:NEW', fields)
    assert exc.value.code == 'forbidden'
    assert w.store._get_or_refuse('demo:NEW')['evidence_kinds'] == ['decision', 'commit']
    assert w.store.verify_integrity() == []


def test_oversight_cannot_declare_decision_only_on_create(w, monkeypatch):
    from test_keeper_oversight_scope import fresh, live, op

    from plangraph.keeper import oversight_policy

    _, principal = live(w, monkeypatch, projects=('demo',))
    item = dict(fresh('NEW', kind='question', tier='none', evidence=['decision']), project='demo')
    with pytest.raises(Refused) as exc:
        w.store._normalise_create(item, principal)
    assert exc.value.code == 'forbidden' and 'router' in exc.value.msg
    verdicts = oversight_policy.check_batch({}, [op('create', item=item)], principal.projects)
    assert any(v.code == 'forbidden' and v.reason == 'decision_only' for v in verdicts)


@pytest.mark.parametrize('who', ['l1main', 'workerA', 'reviewer', 'reader', 'system'])
@pytest.mark.parametrize('include_tier', [False, True])
def test_non_router_cannot_declare_decision_only_on_claimed_item(w, who, include_tier):
    w.store.create_items(w.p('router'), [
        defn('NEW', review_tier='none', evidence_kinds=['decision', 'commit']),
        defn('NEW-GOAL', ['NEW'], kind='goal')])
    w.store.claim(w.p('workerA'), 'demo:NEW')
    before = dict(w.store._get_or_refuse('demo:NEW'))
    events = w.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    fields = {'evidence_kinds': ['decision']}
    if include_tier:
        fields['review_tier'] = 'none'
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p(who), 'demo:NEW', fields)
    assert exc.value.code == 'forbidden'
    assert dict(w.store._get_or_refuse('demo:NEW')) == before
    assert w.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == events
    assert w.store.verify_integrity() == []


@pytest.mark.parametrize('tier', ['none', 'standard', 'high'])
@pytest.mark.parametrize('fields', [{'title': 'ordinary rename'}, {'title': 'security rename'},
                                    {'track': 'deploy'}, {'description': 'legacy edit'}])
def test_unrelated_legacy_edit_preserves_decision_only(w, tier, fields):
    w.store.create_items(w.p('router'), [
        defn('LEGACY', review_tier='none', evidence_kinds=['decision']),
        defn('LEGACY-GOAL', ['LEGACY'], kind='goal')])
    w.conn.execute("UPDATE items SET review_tier=? WHERE key='demo:LEGACY'", (tier,))
    w.store.edit_definition(w.p('router'), 'demo:LEGACY', fields)
    item = w.store._get_or_refuse('demo:LEGACY')
    expected = 'high' if tier == 'standard' and fields in (
        {'title': 'security rename'}, {'track': 'deploy'}) else tier
    assert item['review_tier'] == expected
    assert item['evidence_kinds'] == ['decision']
    assert all(item[field] == value for field, value in fields.items())
