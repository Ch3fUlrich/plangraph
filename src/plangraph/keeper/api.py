"""Threaded stdlib HTTP API over one :class:`Store`.

One ``Store`` and one connection live behind one lock, so SQLite is only ever
touched from one thread at a time; ``ThreadingHTTPServer`` still lets slow
clients wait in parallel.  The authenticated principal always comes from the
bearer token — any ``actor``/``role`` in the body is ignored.

Routes (contract):

* ``POST /v1/<write-verb>``   JSON body in, JSON out
* ``POST /v1/{attempts,park,complete,alarms}`` and ``/v1/locks/<repo>/release``
  answer **204 with no body**; ``/v1/locks/<repo>/acquire`` is 200 ``{token, owner}``
* ``GET  /v1/{ready,show,blocked,waves,critical_path,percent,alerts,digest,
  snapshot,whoami,health}`` and ``/v1/audit-meta`` (router/L1)
* ``GET /v1/health`` is open; everything else needs ``Authorization: Bearer``.
* ``?fmt=text`` gives the compact form (the default for ``ready``, ``show``,
  ``alerts`` and ``blocked``); otherwise JSON.
* Refusals are 4xx ``{"error": code, "msg": …}``; unexpected failures are a 500
  with no internals.  Bodies over 1 MiB are rejected with 413.

After committed writes the server rewrites ``plan.snapshot.json`` atomically (temp
file + ``os.replace``): synchronously, or with ``snapshot_debounce_s > 0`` off the
request path with bounded staleness (``snapshot_writer``).  A background reaper
thread calls ``Store.reap`` every 60 s.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import urllib.parse
from contextlib import contextmanager, suppress
from copy import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import SimpleQueue

from .. import inbox, views
from . import timing
from .snapshot_io import SnapshotPublication, write_snapshot  # noqa: F401  (re-exported; M2B signing)
from .snapshot_writer import Reaper, SnapshotWriter
from . import api_routes, audit_meta, auth, brief, changelog, db, fmt
from .auth import Principal, RunPrincipal
from .errors import ConnectionLost, Refused

__all__ = ["MAX_BODY", "KeeperHTTPServer", "KeeperHandler", "Reaper",
           "make_server", "write_snapshot"]

MAX_BODY = 1024 * 1024  # 1 MiB
from .api_routes import READ_VERBS as _READ_VERBS, STATUS as _STATUS
from .api_routes import (READ_CALLS, ROLE_CHECKED_VERBS, ROUTER_VERBS,
                         TEXT_DEFAULT as _TEXT_DEFAULT)
from .api_routes import WRITE_VERBS as _WRITE_VERBS
_READ_VERBS.add("hosts")
_COMMITTED_READS = frozenset({"show", "ready", "snapshot"})
log = logging.getLogger("plangraph.keeper")


def _probe_endpoint(endpoint):
    try:
        import urllib.request
        req = urllib.request.Request(endpoint.rstrip("/") + "/v1/health")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def _host_row(host):
    """The stored columns of one Host record; key material never leaves the object."""
    return {"host": host.host, "node_id": host.node_id, "key_id": host.key_id,
            "endpoint": host.endpoint, "health": host.health,
            "last_seen": host.last_seen}


# --------------------------------------------------------------------------- #
# request handler
# --------------------------------------------------------------------------- #

class KeeperHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "keeper/1.0"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        """Never log a request line: it can carry a query and must stay quiet."""
    # ---------------------------------------------------------------- verbs
    def do_GET(self):  # noqa: N802 - stdlib signature
        self._request("GET")

    def do_POST(self):  # noqa: N802 - stdlib signature
        self._request("POST")

    def _request(self, method):
        """Start of every request: at most one fail line and one status line (I1, V4)."""
        self._logged = False                    # the one-line rule is per request
        self._sent = False                      # and so is "answered" (keep-alive)
        try:
            self._handle(method)
        except Exception as exc:  # noqa: BLE001 - I1: a request never dies silently
            self._escape(exc)

    def _escape(self, exc):
        """Last resort when something escaped ``_handle``: one fail line; one 500 if nothing was sent."""
        self._fail_line(exc)                    # V4: the failed request gets its line even after ``_sent``
        if not (getattr(self, "_sent", False) or getattr(self, "_headers_buffer", None)):
            with suppress(Exception):
                self._json(500, {"error": "internal", "msg": "internal server error"})

    def _handle(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        if method == "GET" and parsed.path == "/inbox":
            return self._inbox()
        if not parsed.path.startswith("/v1/"):
            self._json(404, {"error": "not_found", "msg": "unknown path"})
            return
        parts = [p for p in parsed.path[len("/v1/"):].split("/") if p]
        if not parts:
            self._json(404, {"error": "not_found", "msg": "unknown path"})
            return
        name, extra = parts[0], "/".join(parts[1:])
        query = urllib.parse.parse_qs(parsed.query)

        if name == "health":
            conn = getattr(self.server.store, "conn", None)
            if getattr(conn, "lost", False) or getattr(self.server, "fatal_reason", None):
                self._json(503, {"status": "unavailable"})   # I11: flag only, no IO, no lock
            else:
                self._json(200, {"status": "ok"})
            return

        allowed = _READ_VERBS if method == "GET" else _WRITE_VERBS
        if name not in allowed:
            known = name in (_READ_VERBS | _WRITE_VERBS)
            self._json(405 if known else 404,
                       {"error": "method_not_allowed" if known else "not_found",
                        "msg": f"no route {method} /{name}"})
            return

        server = self.server
        if method == "GET" and name in _COMMITTED_READS and getattr(server, "read_path", None):
            token = self._token()
            # Scoped oversight credentials can charge a denial budget during authentication.
            if not (token or "").startswith(auth.OVERSIGHT_PREFIX) and not server.fatal_reason:
                return self._committed_read(name, extra, query, token)
        try:
            with server.lock:
                try:
                    principal = auth.authenticate(server.store.conn, self._token())
                    revoked = None
                except Refused as exc:
                    if method != "POST" or not self._can_read_revoked_receipt(name, exc):
                        self._auth_failure(name, method, exc)
                        return
                    principal, revoked = exc.principal, exc

            # Socket reads stay outside the shared keeper lock.
            try:
                body = self._read_body() if method == "POST" else {}
            except Refused:
                if revoked is None:
                    raise
                with server.lock:
                    self._auth_failure(name, method, revoked)
                return
            if body is None:
                return  # 413 already sent
            with timing.RequestTimer(server.lock, "read" if method == "GET" else "write", verb=name):
                if revoked is not None:
                    self._read_revoked_receipt(name, principal, body, revoked)
                    return
                api_routes.run_gate(principal, name)
                if method == "GET":
                    payload, as_text = self._read(name, extra, query, principal)
                else:
                    payload, replayed = self._request_write(name, body or {}, principal, extra)
                    as_text = query.get("fmt", ["json"])[0] == "text"
                    receipt_read = name == "write_receipt" and (
                        payload.get("result") == "duplicate" or payload.get("http_replayed"))
                    preview = name == "close_merged" and body.get("dry_run") is True
                    merge_preview = name == "close_merged_auto" and (
                        "refresh" in body or body.get("repos_only") or body.get("apply") is False)
                    if not (replayed or receipt_read or preview or merge_preview):
                        self._snapshot(principal)
                    if payload is None:   # worker verbs: 204, no body
                        self._empty(204)
                        return
                if as_text:
                    self._text(200, payload if isinstance(payload, str)
                               else fmt.result_text(name, payload))
                else:
                    self._json(200, payload)
        except Refused as exc:
            self._json(_STATUS.get(exc.code, 400), {"error": exc.code, "msg": exc.msg})
        except ConnectionLost as exc:  # I1/I4: fail loud, never a silent close
            self._fail_line(exc)
            msg = ("keeper database lost at commit; outcome unknown"
                   if exc.phase == "commit" else "keeper database unavailable")
            try:
                self._json(503, {"error": "unavailable", "msg": msg})
            finally:               # F2: a dead client must not skip the fatal
                server.fail("postgres connection lost")
        except Exception as exc:  # noqa: BLE001 - never leak internals
            self._fail_line(exc)
            self._json(500, {"error": "internal", "msg": "internal server error"})

    def _committed_read(self, name, extra, query, token):
        server = self.server
        try:
            with timing.RequestTimer(None, "read", verb=name), server.read_store() as store:
                try:
                    principal = auth.authenticate(store.conn, token)
                except Refused as exc:
                    self._auth_failure(name, "GET", exc, store=store)
                    return
                api_routes.run_gate(principal, name)
                payload, as_text = self._read(name, extra, query, principal, store=store)
            (self._text if as_text else self._json)(200, payload)
        except Refused as exc:
            self._json(_STATUS.get(exc.code, 400), {"error": exc.code, "msg": exc.msg})
        except ConnectionLost as exc:
            self._fail_line(exc)
            self._json(503, {"error": "unavailable", "msg": "keeper read connection unavailable"})

    @staticmethod
    def _can_read_revoked_receipt(name, exc):
        return isinstance(exc, auth.RevokedRunToken) and (
            name == "write_receipt" or exc.principal.token_role == "reviewer"
            and name in ("replay", "verdict", "bind_seat_verdict")
        )

    def _read_revoked_receipt(self, name, principal, body, refusal):
        store = self.server.store
        if name == "write_receipt":
            settled = store.read_revoked_write_receipt(principal, body)
        elif name == "replay":
            settled = store.read_revoked_seat_replay(principal, body)
        else:
            settled = store.read_revoked_seat_verdict(principal, body)
            if name == "bind_seat_verdict" and settled is not None:
                settled = {"binding": body["binding"]}
        if settled is None:
            self._auth_failure(name, "POST", refusal)
        else:
            self._json(200, settled)

    def _auth_failure(self, name, method, exc, *, store=None):
        if method == "POST":
            self.close_connection = True   # Never interpret an unread body as another request.
        with suppress(Exception):          # Missing or broken audit still returns the refusal.
            (store or self.server.store).audit_auth_failure(exc.msg or exc.code)
        self._json(403 if name == "ingest_host_sample" else 401, {"error": exc.code, "msg": exc.msg})

    # ------------------------------------------------------------- plumbing
    def _fail_line(self, exc):
        """One stderr line per failed request: the type only (I10), never ``str(exc)``."""
        if getattr(self, "_logged", False):
            return                          # the request path already logged this request (V4)
        self._logged = True
        with suppress(Exception):           # stderr trouble must not mask the response (V4)
            print(f"keeper: request failed: {type(exc).__name__}", file=sys.stderr)

    def _token(self):
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[len("Bearer "):].strip()
        return None

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > MAX_BODY:
            self._drain(length)
            self._json(413, {"error": "too_large",
                             "msg": f"body exceeds {MAX_BODY} bytes"})
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw or raw.strip(b" \t\r\n") == b"null":
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise Refused("validation", "body is not valid JSON")

    def _drain(self, length):
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _snapshot(self, principal):
        writer = self.server.snapshot_writer
        if writer is not None:     # debounced: rebuilt off the request path (snapshot_writer)
            writer.mark()
            return
        try:
            write_snapshot(self.server, principal)
        except Exception:  # a committed write stays committed
            pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _text(self, status, text):
        if not isinstance(text, str):
            text = json.dumps(text)
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _empty(self, status):
        """No body, no Content-Length/Content-Type (HTTP 204 must not carry one)."""
        self.send_response(status)
        self._sent = True                     # only a written status line counts (I1)
        self.end_headers()

    def _inbox(self):
        """GET /inbox: the static, unauthenticated question form."""
        self._send(200, inbox.routes.page(), "text/html; charset=utf-8",
                   inbox.routes.PAGE_HEADERS)

    def _send(self, status, body, ctype, headers=()):
        if status == 413:
            self.close_connection = True
        self.send_response(status)
        self._sent = True                     # only a written status line counts (I1)
        self.send_header("Content-Type", ctype)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    # ---------------------------------------------------------------- reads
    @staticmethod
    def _one(query, name, default=None):
        values = query.get(name)
        return values[0] if values else default

    @timing.measured(part="query_ms")
    def _read(self, name, extra, query, principal, *, store=None):
        if name == "write_context":
            return self.server.store.write_context(principal, query.get("key", [None])[0]), False
        if name in ("host_samples", "host_samples_range"): return __import__("plangraph.keeper.store_host_samples", fromlist=["http_read"]).http_read(self.server.store, principal, name, query), False
        if name == "hosts":
            registry = getattr(self.server, "bus_registry", getattr(self.server.store, "bus_registry", None))
            hosts_data = []
            if registry is not None:
                # BUS-REGISTRY-PERSIST: the table is the served source (it
                # survives a restart) and the current inventory decides who is
                # served -- a row whose host is no longer provisioned is never
                # served.  Startup restore forces the persisted health to
                # 'offline', so a pre-restart 'healthy' is never shown.
                persisted = {row["host"]: row
                             for row in self.server.store.list_bus_hosts()}
                for h in registry.hosts.values():
                    row = persisted.get(h.host) or {}
                    hosts_data.append({
                        "host": h.host,
                        "endpoint": row.get("endpoint", h.endpoint),
                        "key_id": h.key_id,
                        "health": row.get("health", h.health),
                        "last_seen": row.get("last_seen", h.last_seen),
                        # True only when a durable row backs this entry; the
                        # startup seed makes that every inventory host.
                        "persisted": bool(row),
                    })
            return {"hosts": hosts_data}, False
        if name == "messages":
            from .messages_poll import read_messages
            return read_messages(self.server, principal, query), False
        store = store if store is not None else self.server.store
        fmt_name = self._one(query, "fmt")
        as_text = fmt_name == "text" or (
            fmt_name is None and name in _TEXT_DEFAULT)
        project = self._one(query, "project")

        if name in inbox.routes.READ_VERBS:
            return inbox.routes.read(store, principal, name, query), False
        if name == "attempts":
            return store.spawn_attempts(principal, self._one(query, "since", "1d"), scope=self._one(query, "scope")), False
        if name == "ready":
            n = None if self._one(query, "all") else self._int(self._one(query, "n", 10), "n")
            mine = str(self._one(query, "mine")).lower() in ("1", "true")   # MCP sends "True"
            agent = str(self._one(query, "agent")).lower() in ("1", "true")
            owner = principal.name if mine else self._one(query, "owner")
            if as_text:
                return store.ready(principal, project=project, n=n, owner=owner, agent=agent), True
            return store.ready_data(principal, project=project, n=n, owner=owner, agent=agent), False
        if name == "show":
            key = store.resolve_key(self._one(query, "key") or extra)
            return brief.show(store, principal, key,
                              self._one(query, "brief"), as_text, fmt.show_text)
        if name == "blocked":
            data = store.blocked(principal, project=project)
            return (fmt.blocked_text(data), True) if as_text else (data, False)
        if name == "alerts":
            data = store.alerts(principal, project=project)
            return (fmt.alerts_text(data), True) if as_text else (data, False)
        if name == "waves":
            data = store.waves(principal)
            return (fmt.waves_text(data), True) if as_text else (data, False)
        if name == "critical_path":
            data = store.critical_path(principal, project=project)
            return (fmt.critical_text(data), True) if as_text else (data, False)
        if name == "percent":
            by = self._one(query, "by", "estimate")
            data = store.percent(principal, by=by, project=project)
            return (fmt.percent_text(data), True) if as_text else (data, False)
        if name == "digest":
            data = store.digest(principal)
            return (fmt.digest_text(data), True) if as_text else (data, False)
        if name == "snapshot":
            data = store.snapshot(principal)
            return (fmt.snapshot_text(data), True) if as_text else (data, False)
        if name == "events":
            since = self._int(self._one(query, "since", 0), "since")
            limit = self._int(self._one(query, "limit", 100), "limit")
            return store.event_page(principal, since, limit), False
        if name == "changelog":
            data = store.changelog(principal, self._one(query, "since"))
            return (changelog.render(data), True) if as_text else (data, False)
        if name == "audit-meta":
            data = store.audit_meta(principal, project=project,
                                    stale_days=self._one(query, "stale_days"))
            return (audit_meta.render(data), True) if as_text else (data, False)
        if name == "metrics":
            return (store.get_metrics(principal, day=self._one(query, "day"),
                                      since=self._one(query, "since"),
                                      metric=self._one(query, "metric")), False)
        if name in READ_CALLS:
            data = READ_CALLS[name](store, principal, project)
            text = getattr(fmt, name + "_text")
            return (text(data), True) if as_text else (data, False)
        raise Refused("not_found", f"no read verb {name!r}")
    @timing.measured(part="query_ms")
    def _request_write(self, name, body, principal, extra):
        request_key = self.headers.get("Idempotency-Key") if hasattr(self, "headers") else None
        if request_key is None:
            return self._write(name, body, principal, extra), False
        if name not in api_routes.RETRYABLE_WRITES:
            raise Refused("validation", "idempotency key is unsupported for this write verb")
        api_routes.check_write_key(request_key)
        return self.server.store.retry_http_write(
            principal, auth.hash_token(self._token()), request_key, name, extra, body,
            lambda: self._write(name, body, principal, extra))

    def _write(self, name, body, principal, extra=""):
        if name == "close_merged_auto":
            if "refresh" in body:
                self.server.store._require_role(principal, "system", "router")
                return self.server.merge_refresh.control(body["refresh"])
            if body.get("repos_only") is not True:
                self.server.merge_refresh.consume()
        if name in ("ingest_host_sample", "mint_host_token", "revoke_host_token"): return __import__("plangraph.keeper.store_host_samples", fromlist=["http_write"]).http_write(self.server.store, principal, name, body)
        if name == "write_receipt":
            return self.server.store.write_receipt(principal, body)
        if name == "post_message":
            if not isinstance(body, dict):
                raise Refused("validation", "body must be a JSON object")
            if "principal" in body or "registry" in body:
                raise Refused("validation", "body contains reserved keys")
            registry = getattr(self.server, "bus_registry", getattr(self.server.store, "bus_registry", None))
            return {"row": self.server.store.post_message(principal, registry=registry, fields=body)}
        store = self.server.store
        if not isinstance(body, dict):
            if name in ("create_items", "import_items") and isinstance(body, list):
                body = {"items": body}
            else:
                raise Refused("validation", "body must be a JSON object")
        if name in api_routes.WORKER_VERBS:   # RT-P1-KEEPER: the store resolves the card
            return api_routes.worker_write(store, principal, name, extra, body)
        if name in api_routes.JEV_VERBS:      # RT-P1-JEV-SHADOW: the store resolves card
            return api_routes.jev_write(store, principal, name, body)
        if isinstance(body.get("key"), str):  # old (rehomed) keys resolve
            body["key"] = store.resolve_key(body["key"])
        if name in inbox.routes.WRITE_VERBS:
            return inbox.routes.write(store, principal, name, body)
        if name in ("create_items", "import_items"):
            items = body.get("items")
            if not isinstance(items, list):
                raise Refused("validation", "items must be a list")
            return getattr(store, name)(principal, items)
        if name == "edit_definition":
            return store.edit_definition(principal, self._need(body, "key"),
                                         self._need(body, "fields"))
        if name == "set_priority":
            return store.set_priority(principal, self._need(body, "key"),
                                      self._int(body.get("n", body.get("priority")), "n"))
        if name in ("claim", "claim_batch"):
            lease = body.get("lease_s", body.get("lease"))
            lease = None if lease is None else self._int(lease, "lease_s")
            if name == "claim_batch":
                keys = self._need(body, "keys")
                if isinstance(keys, list) and len(keys) <= 200:
                    keys = [store.resolve_key(key) if isinstance(key, str) else key for key in keys]
                return store.claim_batch(principal, keys, lease_s=lease, force_stale=body.get("force_stale"))
            return store.claim(principal, self._need(body, "key"), lease_s=lease,
                               force_stale=body.get("force_stale"))
        if name == "heartbeat_batch":
            claims = self._need(body, "claims")
            if isinstance(claims, list) and len(claims) <= 200:
                claims = [dict(row, key=store.resolve_key(row["key"]))
                          if isinstance(row, dict) and isinstance(row.get("key"), str) else row
                          for row in claims]
            return store.heartbeat_batch(principal, claims)
        if name == "delegate":
            return store.delegate(principal, self._need(body, "key"),
                                  self._need(body, "worker", "worker_principal"))
        if name == "submit":
            source_round = body.get("source_round")
            if isinstance(principal, auth.RunPrincipal) and type(source_round) is not int:
                raise Refused("validation", "run-token submit requires source_round")
            return store.submit(principal, self._need(body, "key"), self._need(body, "epoch"),
                                source_round=source_round)
        if name in ("heartbeat", "release"):
            return getattr(store, name)(principal, self._need(body, "key"),
                                        self._need(body, "epoch"))
        if name == "incident":
            return store.incident(principal, self._need(body, "key"),
                                  body.get("text"), body.get("evidence"))
        if name == "add_evidence":
            return store.add_evidence(principal, self._need(body, "key"),
                                      self._need(body, "epoch"),
                                      self._need(body, "kind"),
                                      body.get("ref") or {})
        if name == "add_evidence_router":
            return store.add_evidence_router(principal, self._need(body, "key"),
                                             self._need(body, "kind"),
                                             body.get("ref") or {})
        if name == "admit_seat_verdict":
            return store.admit_seat_verdict(principal, body)
        if name == "bind_seat_verdict":
            return store.bind_seat_verdict(principal, self._need(body, "key"), body.get("ok"),
                                           body.get("reason", ""), body.get("binding"))
        if name == "verdict":
            ok = body.get("ok")
            if not isinstance(ok, bool):   # "false" must never pass a review
                raise Refused("validation", "ok is required and must be a boolean")
            return store.verdict(principal, self._need(body, "key"), ok,
                                 body.get("reason") or "", binding=body.get("binding"))
        if name == "cancel":
            return store.cancel(principal, self._need(body, "key"),
                                body.get("reason") or "")
        if name == "waive_second_family":
            return store.waive_second_family(principal, self._need(body, "key"),
                                             self._need(body, "decision_id"))
        if name in ROUTER_VERBS:
            return ROUTER_VERBS[name](store, principal, body, self._need)
        if name in ROLE_CHECKED_VERBS:
            return ROLE_CHECKED_VERBS[name](store, principal, body, self._need)
        if name == "reap":
            return store.reap(principal)
        raise Refused("not_found", f"no write verb {name!r}")

    @staticmethod
    def _int(value, name):  # a malformed number is a 400, never a 500
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            raise Refused("validation", f"{name} must be an integer") from None

    @staticmethod
    def _need(body, *names):
        for name in names:
            if body.get(name) is not None:
                return body[name]
        raise Refused("validation", f"missing field {names[0]!r}")


# --------------------------------------------------------------------------- #
# server + reaper
# --------------------------------------------------------------------------- #

class KeeperHTTPServer(ThreadingHTTPServer):
    """One ``Store`` guarded by one lock, plus a system principal for reaps."""

    daemon_threads = True
    request_queue_size = 32
    allow_reuse_address = True

    def __init__(self, address, store, config, system=None):
        self.store = store
        self.config = config
        self.lock = threading.RLock()
        self.snapshot_publication = SnapshotPublication()
        self.merge_refresh = api_routes.MergeRefresh(self)
        self._audit_thread = None
        self.system = system or Principal("system", "system", [], None)
        self.reaper = None
        self.anchorer = None     # M2B-ANCHOR: set by the daemon when anchors are configured
        self.fatal_reason = None
        self._fatal_lock = threading.Lock()
        self.on_fatal = None
        self.snapshot_writer = None
        debounce = float(getattr(config, "snapshot_debounce_s", 0) or 0)
        if debounce > 0:         # KEEPER-SNAPSHOT-PG: 0 = rewrite synchronously after each write
            self.snapshot_writer = SnapshotWriter(
                self, debounce, getattr(config, "snapshot_max_delay_s", 2.0))
            self.snapshot_writer.start()
        self.bus_registry = None
        self.bus_stop = None
        self.bus_thread = None
        inv_file = os.environ.get("PLANGRAPH_BUS_INVENTORY")
        if inv_file and os.path.isfile(inv_file):
            try:
                from ..fleet_bus import discovery
                inv_data = json.loads(Path(inv_file).read_text("utf-8"))
                hosts = inv_data.get("hosts") or []
                records = [
                    discovery.Host(
                        host=entry["host"],
                        node_id=entry["node_id"],
                        key_id=entry["key_id"],
                        key_file=Path(entry["key_file"])
                    )
                    for entry in hosts
                ]
                agents = inv_data.get("agents") or {}
                port = inv_data.get("port", 8765)
                registry = discovery.Registry(
                    records, agents=agents, port=port,
                    on_refresh=self._persist_bus_hosts)
                # BUS-REGISTRY-PERSIST: the persisted rows load into the hosts
                # before the first probe, and every refresh upserts them again.
                self._restore_bus_hosts(registry)
                self.bus_registry = registry
                self.bus_stop = threading.Event()
                self.bus_thread = threading.Thread(
                    target=registry.run,
                    args=(self.bus_stop, _probe_endpoint),
                    daemon=True,
                    name="bus-registry",
                )
                self.bus_thread.start()
            except Exception as exc:  # noqa: BLE001 - a broken bus never breaks the keeper
                log.error("bus registry setup failed: %s: %s",
                          type(exc).__name__, exc)
                self.bus_registry = None
        super().__init__(address, KeeperHandler)
        self.read_path = getattr(config, "db", None)
        self.read_schema = getattr(store.conn, "schema", None)
        self.read_dsn = getattr(config, "db_options", {}).get("dsn")
        self._read_audits = SimpleQueue()
        self._audit_thread = threading.Thread(target=self._flush_read_audits, name="keeper-read-audit",
                                              daemon=True)
        self._audit_thread.start()

    @contextmanager
    def read_store(self):
        """Run a read against committed rows and deliver its audits through the writer."""
        audits = []
        try:
            with db.read_transaction(self.read_path, schema=self.read_schema, dsn=self.read_dsn) as conn:
                view = copy(self.store)
                view.conn, view._batch = conn, None

                def record(*args, **kw):
                    audits.append(("_audit", args, kw))

                def authentication_failure(reason):
                    audits.append(("audit_auth_failure", (reason,), {"now": view.now()}))

                view._audit, view.audit_auth_failure = record, authentication_failure
                yield view
        finally:
            if audits:
                if self.lock.acquire(blocking=False):
                    try:
                        self._apply_read_audits(audits)
                    finally:
                        self.lock.release()
                else:
                    self._read_audits.put(audits)

    def _apply_read_audits(self, audits):
        for method, args, kw in audits:
            try:
                getattr(self.store, method)(*args, **kw)
            except ConnectionLost:
                self.fail("postgres connection lost")
                return
            except Exception as exc:
                log.error("read audit failed: %s", type(exc).__name__)

    def _flush_read_audits(self):
        while (audits := self._read_audits.get()) is not None:
            with self.lock:
                self._apply_read_audits(audits)

    def fail(self, reason: str) -> None:
        """Record fatal failure once; calls on_fatal hook (I2, S2)."""
        with self._fatal_lock:           # first writer wins (F1): check-and-set is atomic
            if self.fatal_reason is not None:
                return
            self.fatal_reason = reason
        with suppress(Exception):
            print(f"keeper: fatal: {reason}", file=sys.stderr)
        if self.on_fatal is not None:
            with suppress(Exception):
                self.on_fatal()

    def _restore_bus_hosts(self, registry):
        """Load ``bus_hosts`` into the registry before the first probe and seed it.

        ``Registry.restore`` keeps the persisted ``endpoint``/``last_seen`` and
        forces ``health`` to ``offline``; the re-seeded row makes
        ``GET /v1/hosts`` read exactly what the registry holds at any moment.
        """
        try:
            registry.restore(self.store.list_bus_hosts())
            with self.lock:
                self.store.upsert_bus_hosts(
                    [_host_row(host) for host in registry.hosts.values()])
        except ConnectionLost:
            # L1: session loss at the very first bus write must reach fail()
            # (same reason as _persist_bus_hosts below) -- the __init__ setup
            # guard would swallow it silently and leave a keeper serving with
            # a disabled bus. Re-raise so that guard tears down the half-built
            # registry instead of starting its thread on a dead session.
            self.fail("postgres session lost (bus registry)")
            raise

    def _persist_bus_hosts(self, hosts):
        """The registry loop's ``on_refresh`` hook: durable rows, never keys."""
        rows = [_host_row(host) for host in hosts]
        try:
            with self.lock:   # one connection behind one lock (see the class docstring)
                self.store.upsert_bus_hosts(rows)
        except ConnectionLost:
            # F2: the reaper's fail() equivalent (see Reaper.run) -- without it
            # discovery's log swallows the loss and the daemon never exits 75.
            self.fail("postgres session lost (bus registry)")
            return          # never re-raise: discovery must stay non-fatal here

    def start_reaper(self):
        if self.reaper is None:
            self.reaper = Reaper(self)
            self.reaper.start()
        return self.reaper

    def stop_reaper(self):
        if self.reaper is not None:
            self.reaper.stop()
            self.reaper = None

    def flush_snapshot(self):
        """Bring ``plan.snapshot.json`` level with the database (no-op when synchronous)."""
        if self.snapshot_writer is not None:
            self.snapshot_writer.flush()

    def server_close(self):
        if getattr(self, "bus_stop", None) is not None:
            self.bus_stop.set()
        self.stop_reaper()
        writer, self.snapshot_writer = self.snapshot_writer, None
        if writer is not None:
            with suppress(Exception):     # shutdown never hangs on a snapshot error
                writer.stop()
        if self._audit_thread is not None:
            self._read_audits.put(None)
            self._audit_thread.join(timeout=2)
            self._audit_thread = None
        super().server_close()


def make_server(store, config, *, host=None, port=None) -> KeeperHTTPServer:
    """Bind to ``config``'s address and start the reaper (not yet serving)."""
    bind_host = host or config.host
    bind_port = config.port if port is None else port
    if getattr(config, "batch_roots", None):
        store.batch_roots = list(config.batch_roots)  # X7-FIX-B: config wins
    server = KeeperHTTPServer((bind_host, bind_port), store, config)
    server.start_reaper()
    return server
