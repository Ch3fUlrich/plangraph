"""Hand-written stdio MCP server exposing the keeper verbs as tools.

JSON-RPC 2.0 over newline-delimited JSON on stdin/stdout.  There is no MCP SDK
and no keeper import beyond this module's own HTTP client: every tool call is one
request to the keeper API (base URL ``PG_URL``) carrying the bearer token read
from the file named by ``PG_TOKEN_FILE``.  Token values are never logged or
returned.  Protocol version is ``2025-06-18`` and is echoed back when the client
asks for a different one.  Entry point: ``python -m plangraph.keeper.mcp``:
``main()`` switches stdin/stdout to UTF-8 first, since Windows stdio defaults
to cp1252 and a non-ASCII item title would otherwise kill the server.

Tool failures (a refused verb or an unreachable API) are MCP results with
``isError`` true — JSON-RPC errors are reserved for a broken request envelope.
"""
from __future__ import annotations

import io
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from .brief import creation_error
from .mcp_tools import TOOLS, _tool

__all__ = ["serve", "handle", "HttpKeeper", "main", "TOOLS", "PROTOCOL_VERSION"]

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "plangraph-keeper"
SERVER_VERSION = "0.1"
TIMEOUT = 10


#: name -> (HTTP method, /v1/<verb>).  All other args pass through.
_ROUTES = {
    "ready": ("GET", "ready"), "show": ("GET", "show"),
    "claim": ("POST", "claim"), "delegate": ("POST", "delegate"),
    "heartbeat": ("POST", "heartbeat"), "release": ("POST", "release"),
    "evidence": ("POST", "add_evidence"), "submit": ("POST", "submit"),
    "verdict": ("POST", "verdict"), "cancel": ("POST", "cancel"),
    "waive_second_family": ("POST", "waive_second_family"),
    "close_merged": ("POST", "close_merged"),
    "close_on_decision": ("POST", "close_on_decision"),
    "withdraw_review": ("POST", "withdraw_review"),
    "reopen": ("POST", "reopen"),
    "set_kind": ("POST", "set_kind"),
    "requeue": ("POST", "requeue"),
    "answer": ("POST", "answer"),
    "set_legacy_state": ("POST", "set_legacy_state"),
    "apply_ops": ("POST", "apply_ops"),
    "priority": ("POST", "set_priority"), "blocked": ("GET", "blocked"),
    "waves": ("GET", "waves"), "critical_path": ("GET", "critical_path"),
    "percent": ("GET", "percent"), "alerts": ("GET", "alerts"),
    "needs_you": ("GET", "needs_you"), "answers": ("GET", "answers"),
    "audit_meta": ("GET", "audit-meta"),
    "digest": ("GET", "digest"), "whoami": ("GET", "whoami"),
    "create": ("POST", "create_items"), "edit": ("POST", "edit_definition"),
    "record_run": ("POST", "record_run"),
    "mint_run_token": ("POST", "mint_run_token"),
}

#: Tools served by the fleet-bus client itself (signed write, cursor read).
_BUS_TOOLS = ("fleet_msg_send", "fleet_msg_read")


class _Transport(Exception):
    """The API could not be reached at all (a tool error, not a refusal)."""


def _refusal(body, status):
    """Turn a 4xx body into ``(code, msg)``; fall back to the HTTP status."""
    try:
        data = json.loads(body)
    except ValueError:
        return f"http_{status}", (body or "").strip()
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"]), str(data.get("msg", ""))
    return f"http_{status}", str(data)


class HttpKeeper:
    """Bearer-authenticated client for the keeper HTTP API."""

    def __init__(self, base_url, token_path, *, opener=None, timeout=TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.token_path = token_path or ""
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener()

    def _token(self):
        with open(self.token_path, encoding="utf-8") as handle:
            return handle.read().strip()

    def call(self, method, verb, args):
        """Return ``(text, is_error)``; never raise for a refusal."""
        try:
            token = self._token()
            url = f"{self.base_url}/v1/{verb}"
            data = None
            if method == "GET":
                query = {name: value for name, value in args.items()
                         if value is not None}
                query.setdefault("fmt", "text")
                url += "?" + urllib.parse.urlencode(query)
            else:
                data = json.dumps(args).encode("utf-8")
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("Authorization", "Bearer " + token)
            request.add_header("Accept", "text/plain")
            if data is not None:
                request.add_header("Content-Type", "application/json")
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace"), False
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            code, msg = _refusal(body, exc.code)
            return (f"{code}: {msg}" if msg else code), True
        except (urllib.error.URLError, OSError) as exc:
            raise _Transport(f"unreachable: {exc}") from exc


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}


def _result(mid, payload):
    return {"jsonrpc": "2.0", "id": mid, "result": payload}


def _initialize(params):
    requested = params.get("protocolVersion") if isinstance(params, dict) else None
    version = requested if isinstance(requested, str) and requested \
        else PROTOCOL_VERSION
    return {"protocolVersion": version, "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}


def _tool_error(mid, message):
    return _result(mid, {"content": [{"type": "text", "text": message}],
                         "isError": True})


def _call_tool(mid, params, client):
    name = params.get("name") if isinstance(params, dict) else None
    args = params.get("arguments") if isinstance(params, dict) else None
    if name in _BUS_TOOLS:
        # The bus client signs and queues locally; no keeper HTTP round trip.
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _error(mid, -32602, "arguments must be an object")
        from ..fleet_bus import client as bus_client
        try:
            text, is_error = bus_client.call_tool(name, args), False
        except (bus_client.Refused, bus_client.TransportError) as exc:
            text, is_error = str(exc), True
        return _result(mid, {"content": [{"type": "text", "text": text}],
                             "isError": is_error})
    if name not in _ROUTES:
        return _error(mid, -32602, f"unknown tool: {name}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _error(mid, -32602, "arguments must be an object")
    if name == "create":
        items = args.get("items")
        if (not isinstance(items, list) or len(items) > 50
                or any(not isinstance(entry, dict) for entry in items)):
            return _tool_error(
                mid, "validation: items must be a list of objects (<= 50)")
        # The store checks decisions after duplicate/conflict/lock refusals.
        if error := creation_error(
                [item for item in items if item.get("kind") != "decision"]):
            return _tool_error(mid, error)
    elif name == "edit":
        if (not isinstance(args.get("key"), str) or not args.get("key")
                or not isinstance(args.get("fields"), dict)):
            return _tool_error(
                mid, "validation: key must be a string,"
                " fields must be an object")
    method, verb = _ROUTES[name]
    try:
        text, is_error = client.call(method, verb, args)
    except _Transport as exc:
        text, is_error = str(exc), True
    return _result(mid, {"content": [{"type": "text", "text": text}],
                         "isError": is_error})


def handle(message, client):
    """Dispatch one decoded JSON-RPC message; ``None`` for notifications."""
    if not isinstance(message, dict) or "method" not in message:
        return _error(None, -32600, "invalid request")
    method = message["method"]
    mid = message.get("id")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "initialize":
        response = _result(mid, _initialize(params))
    elif method == "ping":
        response = _result(mid, {})
    elif method == "tools/list":
        response = _result(mid, {"tools": list(TOOLS)})
    elif method == "tools/call":
        response = _call_tool(mid, params, client)
    else:
        response = _error(mid, -32601, f"method not found: {method}")
    return response if mid is not None else None


def serve(instream, outstream, client=None):
    """Read newline-delimited JSON from *instream*, write replies to *outstream*."""
    client = client or _default_client()
    for line in instream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = _error(None, -32700, "parse error")
        else:
            response = handle(message, client)
        if response is not None:
            outstream.write(json.dumps(response, ensure_ascii=False) + "\n")
            outstream.flush()




def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cleartext_warning(base_url):
    """A warning when *base_url* would send the bearer token unencrypted.

    Plain ``http`` is fine on loopback (the keeper itself, the ssh relay);
    anywhere else the token must travel inside TLS (PG-PROXY-ACCESS).
    """
    parts = urllib.parse.urlsplit(base_url or "")
    if parts.scheme != "http" or _is_loopback(parts.hostname or ""):
        return None
    return (f"warning: PG_URL {parts.scheme}://{parts.hostname} sends the bearer"
            " token in clear text; use https (keeper proxy) or loopback")


def _default_client():
    url = os.environ.get("PG_URL", "")
    warning = cleartext_warning(url)
    if warning:
        sys.stderr.write(warning + "\n")
    return HttpKeeper(url, os.environ.get("PG_TOKEN_FILE", ""))


def _utf8(stream, errors):
    """Return *stream* switched to UTF-8 — Windows stdio defaults to cp1252.

    ``TextIOWrapper.reconfigure`` where the stream offers it (the real
    ``sys.stdin``/``sys.stdout``), a fresh wrapper over ``stream.buffer`` where
    it does not, and *stream* unchanged when it is a plain text stream: a
    ``StringIO`` handed straight to ``serve()`` carries no encoding at all.
    ``reconfigure`` failures (a detached, closed or mid-read stream) leave the
    stream as it was rather than killing the server.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except (ValueError, OSError, RuntimeError):
            pass
        return stream
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        return stream
    return io.TextIOWrapper(buffer, encoding="utf-8", errors=errors)


def main(argv=None):
    # cp1252 stdout would raise UnicodeEncodeError on any non-ASCII title and
    # drop the MCP client; utf-8 in, utf-8 out, with the reader never fatal
    # ("replace") and the writer never fatal ("backslashreplace").
    serve(_utf8(sys.stdin, "replace"), _utf8(sys.stdout, "backslashreplace"),
          _default_client())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
