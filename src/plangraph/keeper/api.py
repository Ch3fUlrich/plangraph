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
import os
import sys
import tempfile
import threading
import urllib.parse
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import inbox, views
from . import outage
from .snapshot_io import write_snapshot  # noqa: F401  (re-exported; M2B signing)
from .snapshot_writer import SnapshotWriter
from . import api_routes, audit_meta, auth, brief, changelog, fmt
from .auth import Principal, RunPrincipal
from .errors import ConnectionLost, Refused

__all__ = ["MAX_BODY", "KeeperHTTPServer", "KeeperHandler", "Reaper",
           "make_server", "write_snapshot"]

MAX_BODY = 1024 * 1024  # 1 MiB
from .api_routes import READ_VERBS as _READ_VERBS, STATUS as _STATUS
from .api_routes import (READ_CALLS, ROLE_CHECKED_VERBS, ROUTER_VERBS,
                         TEXT_DEFAULT as _TEXT_DEFAULT)
from .api_routes import WRITE_VERBS as _WRITE_VERBS


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
        try:
            self._handle("GET")
        except Exception as exc:  # noqa: BLE001 - I1: a request never dies silently
            self._escape(exc)

    def do_POST(self):  # noqa: N802 - stdlib signature
        try:
            self._handle("POST")
        except Exception as exc:  # noqa: BLE001 - I1: a request never dies silently
            self._escape(exc)

    def _escape(self, exc):
        """Last resort when something escaped ``_handle``: one 500, never a dropped socket."""
        if not getattr(self, "_sent", False):
            self._fail_line(exc)
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

        body = None
        if method == "POST":
            try:
                body = self._read_body()
            except Refused as exc:
                self._json(400, {"error": exc.code, "msg": exc.msg})
                return
            if body is None:
                return  # 413 already sent

        server = self.server
        with server.lock:
            try:
                try:
                    principal = auth.authenticate(server.store.conn, self._token())
                except Refused as exc:
                    audit = getattr(server.store, "audit_auth_failure", None)
                    with suppress(Exception):  # absent or broken: still a 401
                        audit and audit(exc.msg or exc.code)
                    self._json(401, {"error": exc.code, "msg": exc.msg})
                    return
                api_routes.run_gate(principal, name)
                if method == "GET":
                    payload, as_text = self._read(name, extra, query, principal)
                else:
                    payload = self._write(name, body or {}, principal, extra)
                    as_text = query.get("fmt", ["json"])[0] == "text"
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
                self._json(_STATUS.get(exc.code, 400),
                           {"error": exc.code, "msg": exc.msg})
            except ConnectionLost as exc:  # I1/I4: fail loud, never a silent close
                self._fail_line(exc)
                msg = ("keeper database lost at commit; outcome unknown"
                       if exc.phase == "commit" else "keeper database unavailable")
                self._json(503, {"error": "unavailable", "msg": msg})
            except Exception as exc:  # noqa: BLE001 - never leak internals
                self._fail_line(exc)
                self._json(500, {"error": "internal",
                                 "msg": "internal server error"})

    # ------------------------------------------------------------- plumbing
    @staticmethod
    def _fail_line(exc):
        """One stderr line per failed request: the type only (I10), never ``str(exc)``."""
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
        if not raw:
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
        self._sent = True
        self.send_response(status)
        self.end_headers()

    def _inbox(self):
        """GET /inbox: the static, unauthenticated question form."""
        self._send(200, inbox.routes.page(), "text/html; charset=utf-8",
                   inbox.routes.PAGE_HEADERS)

    def _send(self, status, body, ctype, headers=()):
        self._sent = True                     # a status line is going out (I1)
        if status == 413:
            self.close_connection = True
        self.send_response(status)
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

    def _read(self, name, extra, query, principal):
        store = self.server.store
        fmt_name = self._one(query, "fmt")
        as_text = fmt_name == "text" or (
            fmt_name is None and name in _TEXT_DEFAULT)
        project = self._one(query, "project")

        if name in inbox.routes.READ_VERBS:
            return inbox.routes.read(store, principal, name, query), False
        if name == "ready":
            n = None if self._one(query, "all") else self._int(self._one(query, "n", 10), "n")
            mine = str(self._one(query, "mine")).lower() in ("1", "true")   # MCP sends "True"
            agent = str(self._one(query, "agent")).lower() in ("1", "true")
            owner = principal.name if mine else self._one(query, "owner")
            text = store.ready(principal, project=project, n=n, owner=owner, agent=agent)
            if as_text:
                return text, True
            items = views.ready(store._all_items(), project=project, owner=owner, agent=agent)
            if n is not None:
                items = items[:int(n)]
            return {"items": items,
                    "unblocks": views.unblocks(store._all_items())}, False
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
    # --------------------------------------------------------------- writes
    def _write(self, name, body, principal, extra=""):
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
        if name == "claim":
            lease = body.get("lease_s", body.get("lease"))
            return store.claim(principal, self._need(body, "key"),
                               lease_s=None if lease is None else self._int(lease, "lease_s"))
        if name == "delegate":
            return store.delegate(principal, self._need(body, "key"),
                                  self._need(body, "worker", "worker_principal"))
        if name in ("heartbeat", "release", "submit"):
            return getattr(store, name)(principal, self._need(body, "key"),
                                        self._need(body, "epoch"))
        if name == "add_evidence":
            return store.add_evidence(principal, self._need(body, "key"),
                                      self._need(body, "epoch"),
                                      self._need(body, "kind"),
                                      body.get("ref") or {})
        if name == "add_evidence_router":
            return store.add_evidence_router(principal, self._need(body, "key"),
                                             self._need(body, "kind"),
                                             body.get("ref") or {})
        if name == "verdict":
            ok = body.get("ok")
            if not isinstance(ok, bool):   # "false" must never pass a review
                raise Refused("validation", "ok is required and must be a boolean")
            return store.verdict(principal, self._need(body, "key"), ok,
                                 body.get("reason") or "")
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

class Reaper(threading.Thread):
    """Calls ``Store.reap`` every ``config.reap_interval`` seconds."""

    def __init__(self, server):
        super().__init__(name="keeper-reaper", daemon=True)
        self.server = server
        self.interval = getattr(server.config, "reap_interval", 60.0)
        #: NOT ``_stop``: ``Thread`` owns that name; shadowing it breaks join.
        self._halt = threading.Event()

    def run(self):
        while not self._halt.wait(self.interval):
            try:
                with self.server.lock:
                    self.server.store.reap(self.server.system)
                    write_snapshot(self.server, self.server.system)
                    outage.stamp(self.server.config)      # M2B-OUTBOX: liveness for the outage grace
                    if self.server.anchorer is not None:      # M2B-ANCHOR: done transitions + hourly
                        self.server.anchorer.tick(self.server.store.conn)
            except Exception:  # noqa: BLE001 - the reaper never dies loudly
                continue

    def stop(self):
        self._halt.set()
        if self.is_alive():
            self.join(timeout=2)


class KeeperHTTPServer(ThreadingHTTPServer):
    """One ``Store`` guarded by one lock, plus a system principal for reaps."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, config, system=None):
        self.store = store
        self.config = config
        self.lock = threading.RLock()
        self.system = system or Principal("system", "system", [], None)
        self.reaper = None
        self.anchorer = None     # M2B-ANCHOR: set by the daemon when anchors are configured
        self.snapshot_writer = None
        debounce = float(getattr(config, "snapshot_debounce_s", 0) or 0)
        if debounce > 0:         # KEEPER-SNAPSHOT-PG: 0 = rewrite synchronously after each write
            self.snapshot_writer = SnapshotWriter(
                self, debounce, getattr(config, "snapshot_max_delay_s", 2.0))
            self.snapshot_writer.start()
        super().__init__(address, KeeperHandler)

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
        self.stop_reaper()
        writer, self.snapshot_writer = self.snapshot_writer, None
        if writer is not None:
            with suppress(Exception):     # shutdown never hangs on a snapshot error
                writer.stop()
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
