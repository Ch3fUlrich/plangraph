"""Bind provisioned identities to the endpoints discovery actually probes."""
import re


def peer_endpoints(registry, status):
    """Mirror Registry.refresh, including Self precedence and loopback binding.

    A malformed Self fails closed: a Peer could otherwise pose as the local
    node. Only provisioned identities are included in the health probe map.
    """
    status = status if isinstance(status, dict) else {}
    peers = status.get("Peer", {})
    peers = peers if isinstance(peers, dict) else {}
    lookup = dict(peers)
    local = status.get("Self")
    if "Self" in status and not (isinstance(local, dict)
                                 and isinstance(local.get("PublicKey"), str)):
        lookup, local = {}, None
    elif local is not None:
        lookup[local["PublicKey"]] = local
    expected = {}
    for host in registry.hosts.values():
        peer = lookup.get(host.node_id)
        if not isinstance(peer, dict) or peer.get("Online") is not True:
            continue
        name = peer.get("DNSName") or peer.get("HostName")
        if not isinstance(name, str):
            continue
        name = name.rstrip(".")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", name):
            continue
        endpoint = (f"http://127.0.0.1:{registry.port}" if peer is local
                    else f"http://{name}:{registry.port}")
        expected.setdefault(endpoint, {})[host.host] = host.key_id
    return expected
