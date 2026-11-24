"""The generated registry lists public repositories and keeps remote placement closed per host."""
import pytest

from plangraph.fleet_sync import genconfig
from plangraph.spawner import overflow


def host(name='worker', **extra):
    return dict(id=name, transport='ssh', os='linux', caps={'mem_floor_mb': 1},
                roles=['implement'], state_dir='/state', repos={
                    'plangraph': {'root': '/srv/pg'}, 'private-thing': {'root': '/srv/other'}},
                **extra)


def overflow_config(registry):
    return genconfig.generate(registry)['overflow-hosts.json']


def test_registry_without_public_repos_key_lists_this_public_repository():
    config = overflow_config({'self': 'worker', 'hosts': [dict(host(), transport='local')]})
    assert config['public_repos'] == ['/srv/pg']
    assert config['public_repo_ids'] == {'plangraph': '/srv/pg'}


@pytest.mark.parametrize('listed', [[], ['private-thing']])
def test_explicit_public_repos_wins_over_the_default(listed):
    config = overflow_config({'self': 'worker', 'public_repos': listed,
                              'hosts': [dict(host(), transport='local')]})
    assert config['public_repos'] == [{'private-thing': '/srv/other'}[k] for k in listed]


def test_remote_host_repos_follow_the_default_public_list():
    entry = overflow_config({'self': 'me', 'hosts': [host()]})['hosts']['worker']
    assert entry['repos'] == {'plangraph': '/srv/pg'}


@pytest.mark.parametrize('extra,open_', [({}, False), ({'remote_placement': False}, False),
                                         ({'remote_placement': 'yes'}, False),
                                         ({'remote_placement': True}, True)])
def test_generated_host_carries_a_switch_that_defaults_off(extra, open_):
    entry = overflow_config({'self': 'me', 'hosts': [host(**extra)]})['hosts']['worker']
    assert entry['remote_placement'] is open_


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'host_caps', lambda *a: (2048, 3000))
    monkeypatch.setattr(overflow, 'read_readiness', lambda name: dict(
        ok=True, live_runs=0, mem_available_mb=9000, disk_free_gb=50, fresh=True))


@pytest.mark.parametrize('extra,refused', [({}, True), ({'remote_placement': True}, False)])
def test_closed_switch_refuses_every_remote_candidate(fresh, extra, refused):
    config = overflow.generate({'self': 'me', 'hosts': [host(**extra)]})['overflow-hosts.json']
    config['registry'] = True
    row = overflow.ready_records(config)['worker']
    assert (row.get('refusal') == 'remote placement closed') is refused
    decision = overflow.decide(0, 2048, 3000, {'worker': row}, True)
    assert (decision.host is None) is refused


def test_hand_written_config_without_registry_flag_is_unaffected(fresh):
    config = {'hosts': {'worker': dict(ssh='worker')}, 'public_repos': []}
    assert 'refusal' not in overflow.ready_records(config)['worker']
