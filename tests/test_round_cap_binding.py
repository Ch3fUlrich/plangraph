"""Keeper admission and work identity cannot be reset by relabelling an item."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from plangraph.keeper.cli_verbs import Session, Unreachable
from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.round_cap import RoundCapRefused, enforce_round_cap, reserve_round
from plangraph.spawner.service import SpawnService

A, B = 'plangraph:ROUND-CAP-A', 'plangraph:ROUND-CAP-B'


def card(item=A, pr='862', branch='feat/x'):
    return dict(item_key=item, pr=pr, diff_ref=dict(repo='/repo', base='main', head=branch))


def seed(state, n=3):
    from plangraph.spawner.item_key import annotate_item
    for i in range(n):
        record = dict(run_id=f'run-seed-{i}', role='implement', status='completed', writer_launched=True)
        annotate_item(record, card())
        path = state / 'runs' / record['run_id']
        path.mkdir(parents=True)
        (path / 'record.json').write_text(json.dumps(record))


@pytest.fixture
def keeper(monkeypatch):
    items = {item: dict(key=item, state='claimed', claimed_by='worker',
                        lease_until=time.time() + 3600) for item in (A, B)}
    def call(self, method, verb, params=None, body=None):
        if verb == 'show':
            return items.get(params['key'])
        if verb == 'whoami':
            return dict(name='worker', role='worker')
        assert verb == 'alarms'
    monkeypatch.setattr(Session, 'call', call)
    return items


@pytest.mark.parametrize('pr,branch', [('862', 'feat/other'), ('999', 'feat/x')])
def test_fourth_spawn_under_second_item_refused(tmp_path, keeper, pr, branch):
    seed(tmp_path)
    service = SpawnService(object(), state=tmp_path)
    with pytest.raises(RoundCapRefused):
        asyncio.run(service.spawn(card=card(B, pr, branch), prompt='same work'))
    assert len(list((tmp_path / 'runs').glob('*/record.json'))) <= 4


def test_unknown_keeper_item_refused_before_launch(tmp_path, keeper):
    service = SpawnService(object(), state=tmp_path)
    with pytest.raises(SpawnRefused, match='keeper.*claim'):
        asyncio.run(service.spawn(card=card('plangraph:FORGED'), prompt='same work'))
    assert not service.tasks


def test_legitimate_second_item_on_other_branch_allowed(tmp_path, keeper):
    seed(tmp_path)
    asyncio.run(enforce_round_cap(SpawnService(object(), state=tmp_path), card(B, '999', 'feat/y'), 'implement'))


def test_offline_fourth_round_refused_by_work_identity(tmp_path, monkeypatch):
    seed(tmp_path)
    def offline(*a, **kw):
        raise Unreachable('offline')
    monkeypatch.setattr(Session, 'call', offline)
    with pytest.raises(RoundCapRefused) as err:
        asyncio.run(enforce_round_cap(SpawnService(object(), state=tmp_path), card(B), 'implement'))
    assert err.value.payload['rounds'] == 3
    assert 'PR/branch' in err.value.payload['binding']


@pytest.mark.parametrize('change', [dict(state='open'), dict(claimed_by='other'), dict(lease_until=0)])
def test_unclaimed_foreign_or_expired_item_refused(tmp_path, keeper, change):
    keeper[B].update(change)
    with pytest.raises(SpawnRefused, match='keeper.*claim'):
        asyncio.run(enforce_round_cap(SpawnService(object(), state=tmp_path), card(B), 'implement'))


def test_offline_admission_fails_closed_before_cap(tmp_path, monkeypatch):
    def offline(*a, **kw):
        raise Unreachable('offline')
    monkeypatch.setattr(Session, 'call', offline)
    with pytest.raises(SpawnRefused, match='keeper.*unavailable'):
        asyncio.run(enforce_round_cap(SpawnService(object(), state=tmp_path), card(), 'implement'))


def test_pending_rounds_shared_across_items_and_branches(tmp_path):
    from plangraph.spawner.item_key import annotate_item
    seed(tmp_path, 2)
    service = SimpleNamespace(state=tmp_path)
    first = dict(run_id='run-first')
    annotate_item(first, card())
    assert reserve_round(service, first) is not False
    second = dict(run_id='run-second')
    annotate_item(second, card(B, '999', 'feat/x'))
    assert reserve_round(service, second) is False
