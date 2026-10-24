"""Keeper-owned discovery: tailnet identity selects trust, never a display name."""

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .contract import BusError, validate_post


def tailscale_status(run=subprocess.run):
    """Read local status with a deadline; failures revoke current availability."""
    try:
        result = run(['tailscale', 'status', '--json'], check=True,
                     capture_output=True, text=True, timeout=5)
        value = json.loads(result.stdout)
        if isinstance(value, dict) and isinstance(value.get('Peer'), dict):
            return value
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
        pass
    return {}


@dataclass
class Host:
    """Provisioned identity plus ephemeral endpoint, last-seen and health."""

    host: str
    node_id: str
    key_id: str
    key_file: Path
    endpoint: str | None = None
    last_seen: float | None = None
    health: str = 'offline'


class Registry:
    """Resolve only provisioned, currently healthy hosts or explicit agent aliases."""

    def __init__(self, hosts, agents=None, port=8765):
        hosts = list(hosts)
        for attr in ('host', 'node_id', 'key_id'):
            if len({getattr(host, attr) for host in hosts}) != len(hosts):
                raise ValueError('Duplicate registry identity')
        self.hosts = {host.host: host for host in hosts}
        self.agents = dict(agents or {})
        if any(host not in self.hosts for host in self.agents.values()):
            raise ValueError('Unknown agent host')
        if set(self.agents) & set(self.hosts):
            raise ValueError('Ambiguous alias')
        self.port = port
        self.peers = {}

    def refresh(self, status, now, probe):
        """Recheck every host, including stale-online peers, on every iteration."""
        peers = status.get('Peer', {}) if isinstance(status, dict) else {}
        self.peers = peers if isinstance(peers, dict) else {}
        for host in self.hosts.values():
            host.endpoint, host.health = None, 'offline'
            peer = self.peers.get(host.node_id)
            if not isinstance(peer, dict) or peer.get('Online') is not True:
                continue
            # DNS names are routing data, not authorization. Bind trust to node ID.
            name = peer.get('DNSName') or peer.get('HostName')
            if not isinstance(name, str):
                continue
            name = name.rstrip('.')
            if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', name):
                continue
            endpoint = f'http://{name}:{self.port}'
            try:
                healthy = probe(endpoint)
            except Exception:
                healthy = False
            host.health = 'healthy' if healthy is True else 'unhealthy'
            if healthy is True:
                host.endpoint, host.last_seen = endpoint, now

    def resolve(self, identity):
        """Return a healthy endpoint for an agent or host; otherwise raise LookupError."""
        host = self.hosts.get(self.agents.get(identity, identity))
        if host is None or host.health != 'healthy' or host.endpoint is None:
            raise LookupError('Host unavailable')
        return host.endpoint

    def validate_post(self, host_name, body, signature, now, state):
        """Authenticate a write against exactly one provisioned host's file key."""
        host = self.hosts.get(host_name)
        try:
            envelope = json.loads(body)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise BusError('bad_request') from None
        if host is None or not isinstance(envelope, dict) or envelope.get('from') != host.host:
            raise BusError('unknown_kid')

        def key(kid):
            if kid != host.key_id:
                return None
            try:
                secret = Path(host.key_file).read_bytes()
            except OSError:
                return None
            return secret if len(secret) >= 32 else None

        return validate_post(key, body, signature, now, state)

    def run(self, stop, probe, interval=5, discover=tailscale_status, clock=time.time):
        """Run bounded discovery/probe attempts until the stop event is set.

        The transport supplies a deadline-bound authenticated health probe; this
        registry deliberately owns no sockets or sample storage.
        """
        while not stop.is_set():
            self.refresh(discover(), clock(), probe)
            stop.wait(interval)
