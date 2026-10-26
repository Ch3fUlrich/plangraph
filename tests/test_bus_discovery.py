import json
import subprocess

import pytest

from plangraph.fleet_bus.discovery import Host, Registry, tailscale_status
from plangraph.fleet_bus.contract import BusError, canonical_post, sign, new_state


@pytest.fixture
def status():
    return json.loads('''{"Peer":{"node-a":{"HostName":"alpha","DNSName":"alpha.example.test.","TailscaleIPs":["192.0.2.11"],"Online":true,"OS":"linux"},"node-x":{"HostName":"alpha","DNSName":"stranger.example.test.","TailscaleIPs":["192.0.2.12"],"Online":true,"OS":"windows"}}}''')


@pytest.fixture
def registry(tmp_path):
    a = tmp_path / 'a.key'
    b = tmp_path / 'b.key'
    a.write_bytes(b'a' * 32)
    b.write_bytes(b'b' * 32)
    return Registry([Host('alpha', 'node-a', 'ka', a), Host('beta', 'node-b', 'kb', b)],
                    agents={'worker': 'alpha'})


def test_discovery_magicdns_unknown_and_spoof(registry, status):
    registry.refresh(status, 10, lambda endpoint: True)
    assert registry.resolve('worker') == 'http://alpha.example.test:8765'
    assert registry.resolve('alpha') == registry.resolve('worker')
    assert registry.hosts['alpha'].last_seen == 10
    assert registry.peers['node-x']['OS'] == 'windows'
    assert registry.peers['node-a']['TailscaleIPs'] == ['192.0.2.11']
    del status['Peer']['node-a']
    registry.refresh(status, 11, lambda endpoint: True)
    with pytest.raises(LookupError):
        registry.resolve('alpha')


def test_stale_online_flap_and_reconnect(registry, status):
    registry.refresh(status, 10, lambda endpoint: True)
    registry.refresh(status, 11, lambda endpoint: False)
    with pytest.raises(LookupError):
        registry.resolve('alpha')
    registry.refresh(status, 12, lambda endpoint: True)
    assert registry.resolve('alpha')
    status['Peer']['node-a']['Online'] = False
    registry.refresh(status, 13, lambda endpoint: True)
    assert registry.hosts['alpha'].last_seen == 12
    with pytest.raises(LookupError):
        registry.resolve('alpha')


def test_host_bound_keys_and_mismatch(registry):
    envelope = dict(ts=10, nonce='n', msg_id='m', text='hi', to='beta', **{'from': 'alpha'}, kid='ka')
    signature = sign(b'a' * 32, canonical_post(envelope))
    assert registry.validate_post('alpha', json.dumps(envelope).encode(), signature, 10, new_state())
    with pytest.raises(BusError):
        registry.validate_post('beta', json.dumps(envelope).encode(), signature, 10, new_state())
    envelope['kid'] = 'kb'
    with pytest.raises(BusError):
        registry.validate_post('alpha', json.dumps(envelope).encode(), sign(b'b' * 32, canonical_post(envelope)), 10, new_state())


@pytest.mark.parametrize('failure', [FileNotFoundError(), subprocess.CalledProcessError(1, 'tailscale'), 'garbage', '[]', '{"Peer":[]}'])
def test_discovery_failure_degrades(registry, status, failure):
    registry.refresh(status, 10, lambda endpoint: True)
    def run(*args, **kwargs):
        assert args[0] == ['tailscale', 'status', '--json']
        if isinstance(failure, Exception):
            raise failure
        return subprocess.CompletedProcess(args[0], 0, failure)
    registry.refresh(tailscale_status(run), 11, lambda endpoint: True)
    with pytest.raises(LookupError):
        registry.resolve('alpha')


def test_loop_retries(registry, status):
    calls = []
    class Stop:
        def is_set(self):
            return len(calls) == 2
        def wait(self, interval):
            calls.append(interval)
    registry.run(Stop(), lambda endpoint: True, discover=lambda: status, clock=lambda: 10)
    assert registry.resolve('alpha')
    assert len(calls) == 2
