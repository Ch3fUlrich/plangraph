"""Threaded stdlib HTTP API over one :class:`Store`.

One ``Store`` and one connection live behind one lock, so SQLite is only ever
touched from one thread at a time; ``ThreadingHTTPServer`` still lets slow
clients wait in parallel.  The authenticated principal always comes from the
bearer token — any ``actor``/``role`` in the body is ignored.

Routes (contract):

* ``POST /v1/<write-verb>``   JSON body in, JSON out
* ``GET  /v1/{ready,show,blocked,waves,critical_path,percent,alerts,digest,
  snapshot,whoami,health}``
* ``GET /v1/health`` is open; everything else needs ``Authorization: Bearer``.
* ``?fmt=text`` gives the compact form (the default for ``ready``, ``show``,
  ``alerts`` and ``blocked``); otherwise JSON.
* Refusals are 4xx ``{"error": code, "msg": …}``; unexpected failures are a 500
  with no internals.  Bodies over 1 MiB are rejected with 413.

After every committed write the server rewrites ``plan.snapshot.json``
atomically (temp file + ``os.replace``) and a background reaper thread calls
``Store.reap`` every 60 s.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.parse
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import views
from . import auth, fmt
from .auth import Principal
from .errors import Refused

__all__ = ["MAX_BODY", "KeeperHTTPServer", "KeeperHandler", "Reaper",
           "make_server", "write_snapshot"]

MAX_BODY = 1024 * 1024  # 1 MiB

_READ_VERBS = {"ready", "show", "blocked", "waves", "critical_path", "percent",
               "alerts", "digest", "snapshot", "whoami"}
_WRITE_VERBS = {"create_items", "import_items", "edit_definition",
                "set_priority", "claim", "delegate", "heartbeat", "release",
                "add_evidence", "add_evidence_router", "submit", "verdict",
                "cancel", "reap"}
_TEXT_DEFAULT = {"ready", "show", "alerts", "blocked"}

_STATUS = {
    "forbidden": 403, "not_found": 404, "conflict": 409, "locked": 409,
    "epoch_stale": 409, "lease_held": 409, "same_principal": 409,
    "same_family": 409,
}


# --------------------------------------------------------------------------- #
# snapshot rewriting
# --------------------------------------------------------------------------- #

def write_snapshot(server, principal) -> None:
    """Rewrite ``plan.snapshot.json`` atomically (temp file then rename)."""
    path = Path(server.config.snapshot)
    data = server.store.snapshot(principal)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        self._handle("GET")

    def do_POST(self):  # noqa: N802 - stdlib signature
        self._handle("POST")

    def _handle(self, method):
        parsed = urllib.parse.urlsplit(self.path)
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
                principal = auth.authenticate(server.store.conn, self._token())
            except Refused as exc:
                audit = getattr(server.store, "audit_auth_failure", None)
                with suppress(Exception):  # absent or broken: still a 401
                    audit and audit(exc.msg or exc.code)
                self._json(401, {"error": exc.code, "msg": exc.msg})
                return
            try:
                if method == "GET":
                    payload, as_text = self._read(name, extra, query, principal)
                else:
                    payload = self._write(name, body or {}, principal)
                    as_text = query.get("fmt", ["json"])[0] == "text"
                    self._snapshot(principal)
                if as_text:
                    self._text(200, payload if isinstance(payload, str)
                               else fmt.result_text(name, payload))
                else:
                    self._json(200, payload)
            except Refused as exc:
                self._json(_STATUS.get(exc.code, 400),
                           {"error": exc.code, "msg": exc.msg})
            except Exception:  # noqa: BLE001 - never leak internals
                self._json(500, {"error": "internal",
                                 "msg": "internal server error"})

    # ------------------------------------------------------------- plumbing
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

    def _send(self, status, body, ctype):
        if status == 413:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", ctype)
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

        if name == "ready":
            n = None if self._one(query, "all") else int(self._one(query, "n", 10))
            text = store.ready(principal, project=project, n=n)
            if as_text:
                return text, True
            items = views.ready(store._all_items(), project=project)
            if n is not None:
                items = items[:int(n)]
            return {"items": items}, False
        if name == "show":
            key = self._one(query, "key") or extra
            info = store.show(principal, key)
            return (fmt.show_text(info), True) if as_text else (info, False)
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
        if name == "whoami":
            data = store.whoami(principal)
            return (fmt.whoami_text(data), True) if as_text else (data, False)
        raise Refused("not_found", f"no read verb {name!r}")
    # --------------------------------------------------------------- writes
    def _write(self, name, body, principal):
        store = self.server.store
        if not isinstance(body, dict):
            if name in ("create_items", "import_items") and isinstance(body, list):
                body = {"items": body}
            else:
                raise Refused("validation", "body must be a JSON object")
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
                                      int(body.get("n", body.get("priority"))))
        if name == "claim":
            lease = body.get("lease_s", body.get("lease"))
            return store.claim(principal, self._need(body, "key"),
                               lease_s=None if lease is None else int(lease))
        if name == "delegate":
            return store.delegate(principal, self._need(body, "key"),
                                  self._need(body, "worker",
                                             "worker_principal"))
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
            if ok is None:
                raise Refused("validation", "ok is required")
            return store.verdict(principal, self._need(body, "key"), bool(ok),
                                 body.get("reason") or "")
        if name == "cancel":
            return store.cancel(principal, self._need(body, "key"),
                                body.get("reason") or "")
        if name == "reap":
            return store.reap(principal)
        raise Refused("not_found", f"no write verb {name!r}")

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

    def server_close(self):
        self.stop_reaper()
        super().server_close()


def make_server(store, config, *, host=None, port=None) -> KeeperHTTPServer:
    """Bind to ``config``'s address and start the reaper (not yet serving)."""
    bind_host = host or config.host
    bind_port = config.port if port is None else port
    server = KeeperHTTPServer((bind_host, bind_port), store, config)
    server.start_reaper()
    return server
