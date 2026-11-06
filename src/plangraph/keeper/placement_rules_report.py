"""C5 keeper-only projection; C1 Rust owns specs, validation and resolution."""
import json
import unicodedata

from .errors import Refused
from .placement_rules_bridge import call_rules

MAX_HOSTS = 128
MAX_SNAPSHOT_BYTES = 1024 * 1024


def bounded_snapshot(snapshot):
    """Materialize detached JSON data and enforce keeper projection bounds."""
    try:
        raw = json.dumps(snapshot, allow_nan=False, separators=(",", ":"))
        if len(raw.encode()) > MAX_SNAPSHOT_BYTES:
            raise ValueError
        snapshot = json.loads(raw)
        # Reserve the C1 request envelope too: accepted snapshots must fit stdin.
        # Two-byte UTF-8 characters maximize JSON escaping: 6 bytes per 2 input bytes.
        envelope = dict(op="report", snapshot=snapshot, host_class="dispatch_vm",
                        host_id="é" * 32, process={}, env_file={})
        if len(json.dumps(envelope, separators=(",", ":")).encode()) + 1 > 1024 * 1024:
            raise ValueError
        hosts = snapshot["hosts"]
        if not isinstance(hosts, dict) or len(hosts) > MAX_HOSTS:
            raise ValueError
        for host in hosts:
            if (not host.strip() or len(host.encode()) > 64
                    or any(unicodedata.category(c) == "Cc" for c in host)):
                raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise Refused("rules_invalid", "invalid placement rules snapshot") from None
    return snapshot


def build_report(snapshot: dict) -> dict:
    """Not executing-host admission: no registry inference or process/env inputs."""
    snapshot = bounded_snapshot(snapshot)

    def report(host_class="", host_id=""):
        # C1 requires a nonblank transport ID. An empty host map guarantees
        # this sentinel cannot select an override or represent a real machine.
        selected = snapshot if host_id else dict(snapshot, hosts={})
        return call_rules(dict(op="report", snapshot=selected,
                               host_class=host_class, host_id=host_id or "keeper_projection",
                               process={}, env_file={}))

    # Validate ALL stored overrides first, including hosts omitted from class reports.
    hosts = {host: report(host_id=host)["report"] for host in snapshot["hosts"]}
    fleet = report()
    return dict(snapshot=snapshot, specs=fleet["specs"], fleet=fleet["report"],
                classes={name: report(host_class=name)["report"]
                         for name in ("dispatch_vm", "prox")},
                hosts=hosts, context="keeper_only", source="keeper",
                revision=snapshot["revision"])
