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
hook the wake is logged and skipped, never fatal.

Keeper message envelope (POST ``/v1/post_message``)::

    {"from": <host>, "to": <agent>, "body": {"text": <text>},
     "envelope": {"host": <host>, "sig": <hex>, "nonce": <nonce>,
                  "ts": <epoch s>, "msg_id": <uuid4 hex>}}

``sig`` is ``sign(key_file, canonical_post({from, to, ts, nonce, msg_id, kid,
text}))`` with ``kid`` read from the receiving registry's host record; the
envelope travels without key identifiers or key material.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import discovery
from .contract import BODY_MAX, TEXT_MAX, canonical_post, sign

LOGGER = logging.getLogger("plangraph.fleet_bus.client")
DEFAULT_TIMEOUT = 10.0
#: Rejections a retry can heal (fresh nonce/timestamp); everything else is final.
RETRYABLE_CODES = frozenset({"skew", "rate_limited", "replay_window_full"})


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


def _default_spool(env):
    state = env.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "plangraph" / "fleet-msg"


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
                  key_file=Path(key_file), spool=Path(spool),
                  token_file=str(env.get("PG_TOKEN_FILE") or ""),
                  status_file=str(env.get("FLEET_MSG_STATUS_FILE") or ""))


def load_registry(cfg):
    """Provisioned hosts plus agent aliases from the configured inventory."""
    data = _read_json(cfg.inventory, "inventory")
    hosts = data.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        raise Refused("inventory needs a nonempty hosts list")
    try:
        records = [discovery.Host(host=entry["host"], node_id=entry["node_id"],
                                  key_id=entry["key_id"],
                                  key_file=Path(entry["key_file"]))
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


def _health(endpoint, cfg, transport=None):
    """Deadline-bound probe of the peer's open health route: literal True."""
    try:
        (transport or HttpTransport(cfg)).request(endpoint + "/v1/health")
        return True
    except (Refused, TransportError):
        return False


class Spool:
    """One JSON file per queued message: durable before a write, gone after it.

    The file names are the ``msg_id`` values themselves, so a crashed process
    replays exactly one identity; the keeper's dedupe turns any repetition into
    the same acknowledgement.
    """

    def __init__(self, path):
        self.path = Path(path)

    def entries(self):
        queued = []
        for name in sorted(path.name for path in self.path.glob("*.json")):
            try:
                entry = json.loads((self.path / name).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError, RecursionError):
                continue
            if isinstance(entry, dict) and isinstance(entry.get("msg_id"), str):
                queued.append(entry)
        return sorted(queued, key=lambda entry: (entry.get("created") or 0,
                                                 entry["msg_id"]))

    def add(self, entry):
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            target = self.path / (entry["msg_id"] + ".json")
            tmp = self.path / (entry["msg_id"] + ".json.tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(entry, handle, sort_keys=True, ensure_ascii=True)
                handle.write("\n")
            os.replace(tmp, target)
        except OSError as exc:
            raise Refused(f"spool is not writable ({type(exc).__name__})") from None

    def remove(self, msg_id):
        try:
            (self.path / (str(msg_id) + ".json")).unlink()
        except OSError:
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

    def _headers(self):
        headers = {"Accept": "application/json"}
        if self.cfg.token_file:
            try:
                token = Path(self.cfg.token_file).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                raise Refused("token file is unreadable") from None
            headers["Authorization"] = "Bearer " + token
        return headers

    def request(self, url, *, method="GET", payload=None, query=None):
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        headers = self._headers()
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
        """Fresh nonce and timestamp, same ``msg_id``: one retry identity."""
        ts = int(self.clock())
        envelope = {"from": self.cfg.host, "to": entry["to"], "ts": ts,
                    "nonce": self._nonce(), "msg_id": entry["msg_id"],
                    "kid": self.cfg.key_id, "text": entry["text"]}
        signature = sign(self._secret(), canonical_post(envelope))
        payload = {"from": envelope["from"], "to": envelope["to"],
                   "body": {"text": entry["text"]},
                   "envelope": {"host": envelope["from"], "sig": signature,
                                "nonce": envelope["nonce"], "ts": ts,
                                "msg_id": entry["msg_id"]}}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True).encode("utf-8")
        if len(encoded) > BODY_MAX:
            raise Refused(f"message body exceeds the {BODY_MAX}-byte bus cap")
        return payload

    def send(self, agent, text):
        """Queue and write one message; returns a ``msg_id`` receipt."""
        self._check_text(text)
        endpoint = self.resolve(agent)         # refusal before anything queues
        entry = {"msg_id": uuid.uuid4().hex, "host": self.cfg.host,
                 "to": agent, "text": text, "created": int(self.clock()),
                 "attempts": 0}
        payload = self._payload(entry)          # caps and key file checked here
        self.drain()                            # opportunistic backlog retry
        self.spool.add(entry)                   # durable before the first try
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
            self._log(f"no wake hook configured; wake skipped"
                      f" for row {row.get('seq')}")
            return
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
        """Rows from this host's keeper; *since* is a sequence or a timestamp."""
        endpoint = self.resolve(self.cfg.host)
        query = parse_since(since) if since is not None else {}
        query["fmt"] = "json"
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


def build_client(env=None, *, status=None, probe=None, wake=None, log=None):
    """Config, discovery, transport and spool wired into one client."""
    cfg = load_config(env)
    registry = load_registry(cfg)
    transport = HttpTransport(cfg)
    registry.refresh(status if status is not None else _status(cfg),
                     time.time(),
                     probe or (lambda endpoint: _health(endpoint, cfg, transport)))
    return BusClient(cfg, registry, transport, Spool(cfg.spool),
                     wake=wake, log=log)


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
    send = commands.add_parser("send", help="fleet-msg send <agent> <text>")
    send.add_argument("agent", help="agent alias or host name (discovery)")
    send.add_argument("text", help="message text")
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
            result = active.send(args.agent, args.text)
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
