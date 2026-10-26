"""``fleet-msg``: the client half of the build-L1 bus.

A write is signed with the host key *file* (path from config, key material
never in the environment or on the command line) using the fleet-bus v2
canonical form, so the receiving keeper can verify it with the very same
``discovery.Registry.validate_post`` the wire contract uses: one key file, one
replay window, one ``msg_id`` dedupe table.

Addresses resolve through ``fleet_bus.discovery`` (``Registry.resolve``); an
unknown or unhealthy agent is refused before anything is queued.  Every write
is durable in a local spool first and retried with the **same** ``msg_id`` and
a fresh nonce/timestamp, so a retry can never duplicate a message.  A
successful write is handed to an injected ``wake.WakeHook.on_row``; without a
hook the wake is logged once and skipped, never fatal.

Health identity (V4-2): the probe is unauthenticated, so a bare 2xx must not
make a host healthy.  ``GET /v1/health`` reports the peer's registry as
``{"status": "ok", "key_id": {...}}`` (a ``host -> key_id`` map, or one shared
``key_id`` string) and a host is healthy only when the reported ``key_id``
matches the registry ``key_id`` the inventory provisions for it.  A 2xx from
another service never claims our identity, so it is never healthy.

Wake wiring (V5-1): when the ``FLEET_MSG_WAKE`` config key names a JSON file
``{"sessions": {"<recipient>": {"session": ..., "base": ..., "kind": ...}},
"state": <path>}``, ``build_client`` wires the real ``wake.WakeHook`` with a
``wake_http.SyntheticSink`` and a ``wake.WakeState`` file (a relative ``state``
resolves against the config file's directory).  Without the key the client
logs once and skips wakes.

Keeper message envelope (POST ``/v1/post_message``)::

    {"from": <host>, "to": <agent>, "body": {"text": <text>},
     "envelope": {"host": <host>, "sig": <hex>, "nonce": <nonce>,
                  "ts": <epoch s>, "msg_id": <uuid4 hex>}}

``sig`` is ``sign(key_file, canonical_post({from, to, ts, nonce, msg_id, kid,
text}))`` with ``kid`` read from the receiving registry's host record and
``text`` the **original message text** (G1-1: the keeper verifies the raw
``body["text"]``, never a JSON re-encoding of the body); the envelope travels
without key identifiers or key material.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from . import discovery
from .contract import BODY_MAX, TEXT_MAX, canonical_post, sign
from .wake import SessionTarget, WakeHook, WakeState
from .wake_http import SyntheticSink

LOGGER = logging.getLogger("plangraph.fleet_bus.client")
DEFAULT_TIMEOUT = 10.0
#: The unauthenticated health probe gets two seconds; peers probe in parallel.
PROBE_TIMEOUT_S = 2.0
#: Rejections a retry can heal (fresh nonce/timestamp); everything else is final.
RETRYABLE_CODES = frozenset({"skew", "msg_rate_limited", "replay_window_full"})
#: The msg_id doubles as a spool file name, so only uuid4 hex may pass.
MSG_ID_RE = re.compile(r"[0-9a-f]{32}")


class Refused(Exception):
    """A clear, user-facing refusal: nothing was sent, nothing was queued."""


class TransportError(Exception):
    """The peer could not be reached or is unavailable; the spool keeps it."""


def _log(message):
    """Default sink; never prints a payload or key material."""
    LOGGER.warning(message)


def _read_json(path, what):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise Refused(f"{what} is unreadable: {Path(path).name}") from None
    if not isinstance(value, dict):
        raise Refused(f"{what} must hold a JSON object")
    return value


@dataclass
class Config:
    """Everything a write needs: identity, key *path*, spool, peer auth."""

    inventory: Path
    host: str
    key_id: str
    key_file: Path
    spool: Path
    token_file: str = ""
    status_file: str = ""
    timeout: float = DEFAULT_TIMEOUT
    wake_file: str = ""


def _default_spool(env):
    state = env.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "plangraph" / "fleet-msg"


def _key_path(value, base):
    """A relative key_file resolves against the config file's directory."""
    path = Path(value)
    return path if path.is_absolute() else Path(base) / path


def load_config(env=None):
    """Build the config from the environment; only paths, never key bytes."""
    env = os.environ if env is None else env
    inventory = str(env.get("FLEET_MSG_INVENTORY") or "")
    if not inventory:
        raise Refused("FLEET_MSG_INVENTORY is not set (host inventory JSON)")
    host = str(env.get("FLEET_MSG_HOST") or "")
    if not host:
        raise Refused("FLEET_MSG_HOST is not set (this host's fleet-bus name)")
    if env.get("FLEET_MSG_KEY"):
        raise Refused("key material belongs in the key file, not the environment")
    data = _read_json(inventory, "inventory")
    record = next((entry for entry in data.get("hosts", [])
                   if isinstance(entry, dict) and entry.get("host") == host), None)
    if record is None:
        raise Refused(f"host {host!r} is not provisioned in the inventory")
    key_file, key_id = record.get("key_file"), record.get("key_id")
    if not isinstance(key_file, str) or not key_file \
            or not isinstance(key_id, str) or not key_id:
        raise Refused(f"host {host!r} has no key_file/key_id in the inventory")
    spool = env.get("FLEET_MSG_SPOOL") or _default_spool(env)
    return Config(inventory=Path(inventory), host=host, key_id=key_id,
                  key_file=_key_path(key_file, Path(inventory).resolve().parent),
                  spool=Path(spool),
                  token_file=str(env.get("PG_TOKEN_FILE") or ""),
                  status_file=str(env.get("FLEET_MSG_STATUS_FILE") or ""),
                  wake_file=str(env.get("FLEET_MSG_WAKE") or ""))


def load_registry(cfg):
    """Provisioned hosts plus agent aliases from the configured inventory."""
    data = _read_json(cfg.inventory, "inventory")
    hosts = data.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        raise Refused("inventory needs a nonempty hosts list")
    try:
        records = [discovery.Host(host=entry["host"], node_id=entry["node_id"],
                                  key_id=entry["key_id"],
                                  key_file=_key_path(entry["key_file"],
                                                    cfg.inventory.resolve().parent))
                   for entry in hosts]
        agents = data.get("agents") or {}
        port = data.get("port", 8765)
        return discovery.Registry(records, agents=agents, port=port)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Refused("inventory host entries are not usable") from None


def _status(cfg):
    """A configured status snapshot, else the live tailscale view."""
    if cfg.status_file:
        return _read_json(cfg.status_file, "status snapshot")
    return discovery.tailscale_status()


def _identity_ok(data, expected):
    """Does the peer's ``/v1/health`` claim the registry identity we expect?

    The claim is a ``host -> key_id`` map (one entry per registry host) or a
    single ``key_id`` string shared by every host bound to the endpoint.  A
    claim that is present but foreign never passes, so a 2xx from another
    service cannot masquerade as a keeper.  A *missing* claim leaves identity
    unverified: older keepers predate the claim, so it is tolerated (the
    caller logs) rather than treated as hostile — the strict gate activates
    with the keeper half that publishes ``key_id``.  A 2xx response from a
    non-keeper service (missing status == 'ok') is never healthy.
    """
    if not isinstance(data, dict):
        return False
    if data.get("status") != "ok" and "key_id" not in data:
        return False
    claim = data.get("key_id")
    if isinstance(claim, str):
        return bool(expected) and all(kid == claim for kid in expected.values())
    if isinstance(claim, dict):
        return bool(expected) and all(claim.get(host) == kid
                                      for host, kid in expected.items())
    return claim is None and bool(expected)


def _health(endpoint, cfg, transport=None, expected=None):
    """Deadline-bound probe of the peer's open health route.

    The health probe sends no bearer token (open unauthenticated probe) and
    verifies keeper identity via key_id: expected maps host names to key_ids.
    A 2xx from another service is not healthy.
    """
    try:
        t = transport or HttpTransport(replace(cfg, timeout=PROBE_TIMEOUT_S, token_file=""))
        data = t.request(endpoint.rstrip("/") + "/v1/health", auth=False)
    except (Refused, TransportError):
        return False
    return _identity_ok(data, expected or {})


def _peer_endpoints(registry, status):
    """Bind each provisioned host to the endpoint ``Registry.refresh`` probes.

    The derivation mirrors ``discovery.Registry.refresh`` exactly so the probe
    pool knows which registry identity every endpoint must claim.
    """
    peers = status.get("Peer", {}) if isinstance(status, dict) else {}
    peers = peers if isinstance(peers, dict) else {}
    expected = {}
    for host in registry.hosts.values():
        peer = peers.get(host.node_id)
        if not isinstance(peer, dict) or peer.get("Online") is not True:
            continue
        name = peer.get("DNSName") or peer.get("HostName")
        if not isinstance(name, str):
            continue
        name = name.rstrip(".")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", name):
            continue
        endpoint = f"http://{name}:{registry.port}"
        expected.setdefault(endpoint, {})[host.host] = host.key_id
    return expected


class _Prober:
    """Concurrent, identity-checking health probe with a 2 s per-probe budget.

    :meth:`prime` probes every candidate endpoint on its own thread at the
    same time, so a fleet of slow peers costs one probe, not one each (V4-4).
    :meth:`__call__` is the ``probe(endpoint)`` callable ``Registry.refresh``
    takes and answers from the same cache.
    """

    def __init__(self, cfg, expected):
        self.expected = dict(expected)
        self._cfg = replace(cfg, timeout=PROBE_TIMEOUT_S, token_file="")
        self._transport = HttpTransport(self._cfg)
        self._cache = {}
        self._lock = threading.Lock()

    def __call__(self, endpoint):
        with self._lock:
            if endpoint in self._cache:
                return self._cache[endpoint]
        healthy = _health(endpoint, self._cfg, self._transport,
                          self.expected.get(endpoint, {}))
        with self._lock:
            self._cache.setdefault(endpoint, healthy)
        return healthy

    def prime(self):
        threads = [threading.Thread(target=self, args=(endpoint,), daemon=True)
                   for endpoint in self.expected]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(PROBE_TIMEOUT_S + 1.0)


class Spool:
    """One JSON file per queued message: durable before a write, gone after it.

    The file names are the ``msg_id`` values themselves (uuid4 hex only, so a
    hostile id can never leave the directory), so a crashed process replays
    exactly one identity; the keeper's dedupe turns any repetition into the
    same acknowledgement.  A write is durable: the temp file is fsynced and
    the directory fsynced before and after ``os.replace`` (V6-2), the
    directory is ``0700`` and its files ``0600`` on POSIX (V6-3), and a
    corrupt entry is moved to ``corrupt/`` and logged, never skipped silently.
    """

    def __init__(self, path, *, log=None):
        self.path = Path(path)
        self._log = log or _log

    @staticmethod
    def _file_id(msg_id):
        """Validate the id before it is ever used as a file name."""
        if not isinstance(msg_id, str) or not MSG_ID_RE.fullmatch(msg_id):
            raise Refused("spool entry needs a uuid4 hex msg_id")
        return msg_id

    def _mkdir(self):
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self.path, 0o700)

    def _fsync_dir(self):
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return                      # platform without directory fds
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _quarantine(self, name):
        """Move an unusable entry out of the way and say so; never silent."""
        try:
            self._mkdir()
            corrupt = self.path / "corrupt"
            corrupt.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix":
                os.chmod(corrupt, 0o700)
            os.replace(self.path / name, corrupt / name)
        except OSError as exc:
            self._log(f"spool entry {name} corrupt and not moved"
                      f" ({type(exc).__name__})")
            return
        self._log(f"spool entry {name} corrupt: quarantined in corrupt/")

    def _load(self, name):
        """Parse one entry; anything unusable is quarantined, never guessed."""
        try:
            entry = json.loads((self.path / name).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            self._quarantine(name)
            return None
        created = entry.get("created") if isinstance(entry, dict) else None
        text = entry.get("text") if isinstance(entry, dict) else None
        valid = (isinstance(entry, dict)
                 and isinstance(text, str)
                 and MSG_ID_RE.fullmatch(str(entry.get("msg_id") or ""))
                 and isinstance(entry.get("to"), str)
                 and (created is None
                      or (type(created) in (int, float) and math.isfinite(created))))
        if not valid:
            self._quarantine(name)      # a corrupt `created` never crashes
            return None
        return entry

    def entries(self):
        queued = []
        for name in sorted(path.name for path in self.path.glob("*.json")):
            entry = self._load(name)
            if entry is not None:
                queued.append(entry)
        return sorted(queued, key=lambda entry: (entry.get("created") or 0,
                                                 entry["msg_id"]))

    def add(self, entry):
        msg_id = self._file_id(entry.get("msg_id") if isinstance(entry, dict)
                               else None)
        try:
            self._mkdir()
            target = self.path / (msg_id + ".json")
            tmp = self.path / (msg_id + ".json.tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                if os.name == "posix":
                    os.chmod(tmp, 0o600)
                json.dump(entry, handle, sort_keys=True, ensure_ascii=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())   # the tmp file, before ...
            self._fsync_dir()               # ... the directory, before ...
            os.replace(tmp, target)
            self._fsync_dir()               # ... and after os.replace
        except OSError as exc:
            raise Refused(f"spool is not writable ({type(exc).__name__})") from None

    def remove(self, msg_id):
        try:
            (self.path / (self._file_id(msg_id) + ".json")).unlink()
        except (OSError, Refused):
            # A leftover entry only replays one idempotent message.
            pass


class HttpTransport:
    """Bearer-authenticated JSON over the keeper's HTTP API.

    A 4xx (or a retryable contract code) becomes :class:`Refused`; a network
    failure or a 5xx becomes :class:`TransportError` so the spool keeps it.
    """

    def __init__(self, cfg, *, opener=None):
        self.cfg = cfg
        self._opener = opener or urllib.request.build_opener()

    def _headers(self, auth=True):
        headers = {"Accept": "application/json"}
        if auth and self.cfg.token_file:
            try:
                token = Path(self.cfg.token_file).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                raise Refused("token file is unreadable") from None
            headers["Authorization"] = "Bearer " + token
        return headers

    def request(self, url, *, method="GET", payload=None, query=None, auth=True):
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        headers = self._headers(auth=auth)
        data = None
        if payload is not None:
            data = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method,
                                         headers=headers)
        try:
            with self._opener.open(request, timeout=self.cfg.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            code, message = _error_of(raw, exc.code)
            if exc.code >= 500 or code in RETRYABLE_CODES:
                raise TransportError(f"keeper unavailable ({code})") from None
            raise Refused(f"{code}: {message}" if message else code) from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise TransportError(f"unreachable ({type(exc).__name__})") from exc
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeError):
            raise TransportError("malformed keeper response") from None


def _error_of(raw, status):
    try:
        data = json.loads(raw)
    except ValueError:
        return f"http_{status}", " ".join(raw.split())[:96]
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"]), " ".join(str(data.get("msg", "")).split())[:96]
    return f"http_{status}", ""


def _wire_text(body):
    """The signed text of a message body: exactly what the keeper derives.

    A string body is its own text — the original raw text, never a JSON
    re-encoding.  A structured body has no other text than its canonical JSON
    encoding (compact, key-sorted), the same form the keeper re-derives when
    it verifies the envelope.
    """
    return body if isinstance(body, str) else json.dumps(
        body, sort_keys=True, separators=(",", ":"))


class BusClient:
    """Signed writes, cursor reads, and a spool that keeps ``msg_id`` stable."""

    def __init__(self, cfg, registry, transport, spool, *, wake=None, log=None,
                 clock=time.time):
        self.cfg = cfg
        self.registry = registry
        self.transport = transport
        self.spool = spool
        self.wake = wake
        self.clock = clock
        self._log = log or _log
        if self.wake is None:           # unconfigured: log once, never per row
            self._log("no wake hook configured; wake notifications are skipped")

    # ------------------------------------------------------------------ write
    def resolve(self, identity):
        """A healthy endpoint for an agent/host, else a clear refusal."""
        try:
            return self.registry.resolve(identity)
        except LookupError:
            raise Refused(
                f"unknown agent or unavailable host: {identity!r}") from None

    def _nonce(self):
        return uuid.uuid4().hex

    def _secret(self):
        """Read the host key from its file; environment and argv never apply."""
        try:
            raw = Path(self.cfg.key_file).read_bytes()
        except OSError:
            raise Refused(
                f"host key file unreadable: {self.cfg.key_file.name}") from None
        if len(raw) < 32:
            raise Refused("host key file must hold at least 32 bytes")
        return raw

    @staticmethod
    def _check_text(text):
        if not isinstance(text, str) or not text.strip():
            raise Refused("message text must be a nonempty string")
        if len(text) > TEXT_MAX:
            raise Refused(f"text is {len(text)} characters;"
                          f" the bus cap is {TEXT_MAX}")

    def _payload(self, entry):
        """Fresh nonce and timestamp, same ``msg_id``: one retry identity.

        Per G1-1: signed envelope requires body == {"text": <str>} and verifies
        against that str.  Never send a structured signed body.
        """
        ts = int(self.clock())
        text = entry["text"]
        envelope = {"from": self.cfg.host, "to": entry["to"], "ts": ts,
                    "nonce": self._nonce(), "msg_id": entry["msg_id"],
                    "kid": self.cfg.key_id, "text": text}
        signature = sign(self._secret(), canonical_post(envelope))
        payload = {"from": envelope["from"], "to": envelope["to"],
                   "body": {"text": text},
                   "envelope": {"host": envelope["from"], "sig": signature,
                                "nonce": envelope["nonce"], "ts": ts,
                                "msg_id": entry["msg_id"]}}
        body_encoded = json.dumps({"text": text}, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=True).encode("utf-8")
        if len(body_encoded) > BODY_MAX:
            raise Refused(f"message body exceeds the {BODY_MAX}-byte bus cap")
        return payload

    def send(self, agent, text, *, body=None):
        """Queue and write one message; returns a ``msg_id`` receipt.

        Per G1-1: signed envelope requires body == {"text": <str>} and verifies
        against that str.  If ``body`` is passed, it is converted to string
        text (EXTRA).
        """
        if body is not None:
            if isinstance(body, str):
                text = body
            elif isinstance(body, (dict, list)):
                text = json.dumps(body, sort_keys=True, separators=(",", ":"))
        self._check_text(text)
        endpoint = self.resolve(agent)         # refusal before anything queues
        entry = {"msg_id": uuid.uuid4().hex, "host": self.cfg.host,
                 "to": agent, "text": text,
                 "created": int(self.clock()), "attempts": 0}
        self._payload(entry)                   # caps and key file: refuse first
        self.drain()                           # opportunistic backlog retry
        payload = self._payload(entry)         # V6-6: fresh ts/nonce after it
        self.spool.add(entry)                  # durable before the first try
        return self._attempt(entry, endpoint, payload)

    def _attempt(self, entry, endpoint, payload):
        url = endpoint.rstrip("/") + "/v1/post_message"
        try:
            data = self.transport.request(url, method="POST", payload=payload)
        except TransportError as exc:
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            self.spool.add(entry)
            self._log(f"message {entry['msg_id']} queued: {exc}")
            return {"msg_id": entry["msg_id"], "queued": True,
                    "row": None, "seq": None}
        except Refused:
            self.spool.remove(entry["msg_id"])   # final: never retried forever
            raise
        self.spool.remove(entry["msg_id"])
        row = data.get("row") if isinstance(data, dict) else None
        if not isinstance(row, dict):
            self._log(f"message {entry['msg_id']} accepted;"
                      " no row to hand to the wake hook")
            row = None
        elif type(row.get("seq")) is not int:
            self._log(f"message {entry['msg_id']} row has no sequence;"
                      " wake skipped")
        else:
            self._wake(row)
        return {"msg_id": entry["msg_id"], "queued": False, "row": row,
                "seq": row.get("seq") if isinstance(row, dict) else None}

    def _wake(self, row):
        if self.wake is None:
            return                      # unconfigured: logged once at startup
        self.wake.on_row(row)                   # the real seam; never raises

    def drain(self):
        """Retry the backlog with the same ``msg_id``; logs, never raises."""
        results = []
        for entry in self.spool.entries():
            msg_id = entry["msg_id"]
            try:
                endpoint = self.resolve(entry.get("to", ""))
                payload = self._payload(entry)
            except Refused as exc:
                self._log(f"message {msg_id} still queued: {exc}")
                results.append({"msg_id": msg_id, "queued": True, "row": None,
                                "seq": None, "error": str(exc)})
                continue
            try:
                results.append(self._attempt(entry, endpoint, payload))
            except Refused as exc:              # contract said no
                results.append({"msg_id": msg_id, "queued": False, "row": None,
                                "seq": None, "error": str(exc)})
        return results

    # ------------------------------------------------------------------- read
    def read(self, since=None):
        """Rows from this host's keeper; *since* is a sequence or a timestamp.

        Without a cursor the read starts at sequence 0 (the keeper's
        ``messages`` verb refuses a filter-less read).
        """
        endpoint = self.resolve(self.cfg.host)
        query = parse_since(since) if since is not None else {"since": 0}
        data = self.transport.request(endpoint.rstrip("/") + "/v1/messages",
                                      query=query)
        rows = data.get("rows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise TransportError("keeper returned no message rows")
        return [row for row in rows if isinstance(row, dict)]


def parse_since(value):
    """``--since``: digits are a sequence cursor, an ISO instant or a
    fractional epoch is a timestamp; anything else is refused by name."""
    if type(value) is int and value >= 0:
        return {"since": value}
    if not isinstance(value, str) or not value:
        raise Refused(f"cannot parse since {value!r}:"
                      " use a sequence number or a timestamp")
    if value.isdigit():
        number = int(value)
        if number > 2**63 - 1:
            raise Refused(f"cannot parse since {value!r}: cursor out of range")
        return {"since": number}
    if re.fullmatch(r"[0-9]+\.[0-9]+", value):
        return {"ts_min": int(round(float(value) * 1000))}
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Refused(f"cannot parse since {value!r}:"
                      " use a sequence number or an ISO timestamp") from None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return {"ts_min": int(round(instant.timestamp() * 1000))}


def build_wake(cfg, *, log=None):
    """The configured ``WakeHook``: sessions, HTTP sink, durable state file.

    The ``FLEET_MSG_WAKE`` file names sessions by recipient and may name a
    ``state`` path; a relative one resolves against the config file's
    directory.  Key material is never part of this file.
    """
    data = _read_json(cfg.wake_file, "wake config")
    raw = data.get("sessions")
    if not isinstance(raw, dict) or not raw:
        raise Refused("wake config needs a nonempty sessions map")
    try:
        sessions = {
            name: SessionTarget(session=str(target["session"]),
                                base=str(target["base"]),
                                kind=str(target.get("kind") or "opencode"))
            for name, target in raw.items()
            if isinstance(target, dict)
        }
    except (KeyError, TypeError, ValueError):
        raise Refused("wake sessions need session/base names") from None
    if not sessions:
        raise Refused("wake sessions need session/base names")
    state = str(data.get("state") or "wake.json")
    path = _key_path(state, Path(cfg.wake_file).resolve().parent)
    try:
        return WakeHook(sessions, SyntheticSink(), WakeState(path=path, log=log),
                        log=log)
    except ValueError as exc:
        raise Refused(f"wake config unusable: {exc}") from None


def build_client(env=None, *, status=None, probe=None, wake=None, log=None):
    """Config, discovery, transport and spool wired into one client."""
    cfg = load_config(env)
    registry = load_registry(cfg)
    transport = HttpTransport(cfg)
    status = status if status is not None else _status(cfg)
    prober = _Prober(cfg, _peer_endpoints(registry, status))
    if probe is None:
        prober.prime()                   # peers probe concurrently: ~2 s, not N
    registry.refresh(status, time.time(), probe or prober)
    hook = wake if wake is not None else (
        build_wake(cfg, log=log) if cfg.wake_file else None)
    return BusClient(cfg, registry, transport, Spool(cfg.spool, log=log),
                     wake=hook, log=log)


def call_tool(name, args, env=None, *, wake=None, log=None):
    """The MCP surface of this client: same signing, spool and refusals."""
    args = args if isinstance(args, dict) else {}
    client = build_client(env, wake=wake, log=log)
    if name == "fleet_msg_send":
        to, text = args.get("to"), args.get("text")
        if not isinstance(to, str) or not to:
            raise Refused("to must be a nonempty agent or host name")
        if not isinstance(text, str):
            raise Refused("text must be a string")
        return json.dumps(client.send(to, text), sort_keys=True,
                          ensure_ascii=False, default=str)
    if name == "fleet_msg_read":
        rows = client.read(args.get("since"))
        return json.dumps({"rows": rows}, sort_keys=True, ensure_ascii=False)
    raise Refused(f"unknown bus tool: {name}")


def _parser():
    parser = argparse.ArgumentParser(
        prog="fleet-msg",
        description="Signed fleet-bus client: write to an agent, read the"
                    " local inbox.")
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send", help="fleet-msg send <agent> <text> [--json]")
    send.add_argument("agent", help="agent alias or host name (discovery)")
    send.add_argument("text", help="message text, or a JSON document with --json")
    send.add_argument("--json", dest="as_json", action="store_true",
                      help="validate TEXT as JSON and serialize canonical JSON string as message text")
    read = commands.add_parser("read", help="fleet-msg read [--since <seq|time>]")
    read.add_argument("--since", default=None, metavar="SEQ|TIME",
                      help="sequence number, ISO timestamp or epoch seconds")
    return parser


def main(argv=None, *, client=None):
    """CLI entry: exit 0 sent/read, 2 refusal, 1 transport failure."""
    args = _parser().parse_args(argv)
    try:
        active = client if client is not None else build_client()
        if args.command == "send":
            text = args.text
            if args.as_json:
                try:
                    parsed = json.loads(args.text)
                except ValueError:
                    raise Refused("--json text is not valid JSON") from None
                text = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
            result = active.send(args.agent, text)
            if result["queued"]:
                print(f"queued {result['msg_id']} (will retry)")
            else:
                print(f"sent {result['msg_id']} seq={result['seq']}")
        else:
            rows = active.read(args.since)
            print(json.dumps({"rows": rows}, sort_keys=True,
                             ensure_ascii=False))
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
