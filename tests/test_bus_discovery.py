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


def make_local_registry(tmp_path):
    key = tmp_path / 'a.key'
    key.write_bytes(b'a' * 32)
    return Registry([Host('alpha', 'self-key', 'ka', key)])


def test_self_only_status_resolves_local_host(tmp_path):
    registry = make_local_registry(tmp_path)
    status = {'Self': {'PublicKey': 'self-key', 'ID': 'n123',
                       'DNSName': 'alpha.example.test.', 'HostName': 'alpha', 'Online': True}}
    probed = []
    registry.refresh(status, 10, lambda endpoint: probed.append(endpoint) or True)
    assert registry.resolve('alpha') == 'http://127.0.0.1:8765'
    assert probed == ['http://127.0.0.1:8765']
    assert registry.hosts['alpha'].last_seen == 10
    assert registry.hosts['alpha'].health == 'healthy'
    assert registry.peers == {}


def test_self_offline_stays_offline_and_is_not_probed(tmp_path):
    registry = make_local_registry(tmp_path)
    status = {'Self': {'PublicKey': 'self-key',
                       'DNSName': 'alpha.example.test.', 'HostName': 'alpha', 'Online': False}}
    probed = []
    registry.refresh(status, 10, lambda endpoint: probed.append(endpoint) or True)
    assert probed == []
    assert registry.hosts['alpha'].health == 'offline'
    with pytest.raises(LookupError):
        registry.resolve('alpha')


def test_peer_with_self_dnsname_other_node_id_is_not_local(tmp_path):
    key = tmp_path / 'a.key'
    zkey = tmp_path / 'z.key'
    key.write_bytes(b'a' * 32)
    zkey.write_bytes(b'z' * 32)
    registry = Registry([Host('alpha', 'self-key', 'ka', key), Host('zeta', 'node-z', 'kz', zkey)])
    status = {'Self': {'PublicKey': 'self-key',
                       'DNSName': 'alpha.example.test.', 'HostName': 'alpha', 'Online': False},
              'Peer': {'node-z': {'DNSName': 'alpha.example.test.', 'HostName': 'alpha', 'Online': True}}}
    probed = []
    registry.refresh(status, 10, lambda endpoint: probed.append(endpoint) or True)
    with pytest.raises(LookupError):
        registry.resolve('alpha')
    assert registry.hosts['alpha'].health == 'offline'
    assert registry.hosts['zeta'].endpoint == 'http://alpha.example.test:8765'
    assert 'http://127.0.0.1:8765' not in probed


def test_self_wins_over_peer_with_same_key(tmp_path):
    registry = make_local_registry(tmp_path)
    status = {'Self': {'PublicKey': 'self-key',
                       'DNSName': 'self.example.test.', 'HostName': 'selfhost', 'Online': True},
              'Peer': {'self-key': {'DNSName': 'spoof.example.test.', 'HostName': 'spoof', 'Online': True}}}
    probed = []
    registry.refresh(status, 10, lambda endpoint: probed.append(endpoint) or True)
    assert registry.resolve('alpha') == 'http://127.0.0.1:8765'
    assert probed == ['http://127.0.0.1:8765']
    assert registry.peers == status['Peer']


@pytest.mark.parametrize('bad_self', ['self-key', None, {'DNSName': 'self.example.test.', 'Online': True},
                                      {'PublicKey': 7, 'DNSName': 'self.example.test.', 'Online': True}])
def test_malformed_self_fails_closed_even_with_same_key_peer(tmp_path, bad_self):
    registry = make_local_registry(tmp_path)
    status = {'Self': bad_self,
              'Peer': {'self-key': {'DNSName': 'spoof.example.test.', 'HostName': 'spoof', 'Online': True}}}
    probed = []
    registry.refresh(status, 10, lambda endpoint: probed.append(endpoint) or True)
    with pytest.raises(LookupError):
        registry.resolve('alpha')
    assert probed == []


def test_tailscale_status_accepts_self_only_json():
    def run(*args, **kwargs):
        assert args[0] == ['tailscale', 'status', '--json']
        return subprocess.CompletedProcess(args[0], 0, json.dumps(
            {'Self': {'PublicKey': 'self-key', 'Online': True}}))
    value = tailscale_status(run)
    assert value['Self']['PublicKey'] == 'self-key'
    def run_neither(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, json.dumps({'BackendState': 'Running'}))
    assert tailscale_status(run_neither) == {}


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
