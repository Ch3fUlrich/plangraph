"""Keeper-owned discovery: tailnet identity selects trust, never a display name."""

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .contract import BusError, validate_post

log = logging.getLogger("plangraph.fleet_bus")


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

    def __init__(self, hosts, agents=None, port=8765, on_refresh=None):
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
        self.on_refresh = on_refresh

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
        self._fire_on_refresh()

    def _fire_on_refresh(self):
        """Publish every refresh to ``on_refresh`` (the keeper's persistence hook).

        An exception is logged and never fatal: a broken persistence seam must
        never kill discovery.
        """
        if self.on_refresh is None:
            return
        try:
            self.on_refresh(list(self.hosts.values()))
        except Exception as exc:  # noqa: BLE001 - persistence never kills discovery
            log.error("registry on_refresh failed: %s: %s",
                      type(exc).__name__, exc)

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

    def restore(self, rows):
        """Load persisted rows into the hosts before the first probe.

        ``endpoint`` and ``last_seen`` are restored exactly as stored, but
        ``health`` is forced to ``offline``: ``resolve`` must never trust a
        pre-restart ``healthy`` before the next probe confirms it.  Rows for
        hosts outside the provisioned inventory are ignored.
        """
        for row in rows or ():
            host = self.hosts.get(row.get("host")) if isinstance(row, dict) else None
            if host is None:
                continue
            host.endpoint = row.get("endpoint")
            host.last_seen = row.get("last_seen")
            host.health = 'offline'

    def run(self, stop, probe, interval=5, discover=tailscale_status, clock=time.time,
            on_refresh=None):
        """Run bounded discovery/probe attempts until the stop event is set.

        The transport supplies a deadline-bound authenticated health probe; this
        registry deliberately owns no sockets or sample storage.

        ``on_refresh(hosts)`` (or the ``on_refresh`` given at construction) is
        called after every refresh -- the keeper's ``bus_hosts`` persistence
        hook; it replaces the previous hook and an exception in it is logged
        and never fatal.
        """
        if on_refresh is not None:
            self.on_refresh = on_refresh
        while not stop.is_set():
            self.refresh(discover(), clock(), probe)
            stop.wait(interval)
