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

__all__ = ["serve", "handle", "HttpKeeper", "main", "TOOLS", "PROTOCOL_VERSION"]

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "plangraph-keeper"
SERVER_VERSION = "0.1"
TIMEOUT = 10


def _tool(name, description, props=None, required=()):
    schema = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = list(required)
    return {"name": name, "description": description, "inputSchema": schema}


#: Terse on purpose: tools/list is paid for on every session.
TOOLS = (
    _tool("requeue", "Router: revive a parked card and retire its exec run.",
          {"key": {"type": "string"}, "reason": {"type": "string"}}, ("key",)),
    _tool("ready", "Open items whose deps are done; mine=your items, agent=no operator steps.",
          {"project": {"type": "string"}, "n": {"type": "integer"}, "owner": {"type": "string"},
           "mine": {"type": "boolean"}, "agent": {"type": "boolean"}}),
    _tool("show", "One item, compact; brief=true for the short task brief.",
          {"key": {"type": "string"}, "brief": {"type": "boolean"}}, ("key",)),
    _tool("claim", "Claim a ready item; optional lease seconds.",
          {"key": {"type": "string"}, "lease_s": {"type": "integer"}}, ("key",)),
    _tool("delegate", "Hand a claimed item to a worker principal.",
          {"key": {"type": "string"}, "worker": {"type": "string"}},
          ("key", "worker")),
    _tool("heartbeat", "Extend a lease at the given epoch.",
          {"key": {"type": "string"}, "epoch": {"type": "integer"}},
          ("key", "epoch")),
    _tool("release", "Return a claimed item to open.",
          {"key": {"type": "string"}, "epoch": {"type": "integer"}},
          ("key", "epoch")),
    _tool("evidence", "Attach evidence {kind, ref} to a claimed item.",
          {"key": {"type": "string"}, "epoch": {"type": "integer"},
           "kind": {"type": "string"}, "ref": {"type": "object"}},
          ("key", "epoch", "kind", "ref")),
    _tool("submit", "Submit claimed work for review.",
          {"key": {"type": "string"}, "epoch": {"type": "integer"}},
          ("key", "epoch")),
    _tool("verdict", "Reviewer passes or requests changes.",
          {"key": {"type": "string"}, "ok": {"type": "boolean"},
           "reason": {"type": "string"}}, ("key", "ok")),
    _tool("waive_second_family", "Router: waive the second-family rule (verified decision).",
          {"key": {"type": "string"}, "decision_id": {"type": "string"}},
          ("key", "decision_id")),
    _tool("withdraw_review", "Router: take a decision-closable item out of review (decision names it in Closes:).",
          {"key": {"type": "string"}, "decision_id": {"type": "string"}},
          ("key", "decision_id")),
    _tool("reopen", "Router: take an in_review item back to its claimant at epoch+1 (reason + recorded decision; never closes).",
          {"key": {"type": "string"}, "decision_id": {"type": "string"},
           "reason": {"type": "string"}}, ("key", "decision_id", "reason")),
    _tool("set_kind", "Router: turn an open lane into a question or operator_task (waits on the operator).",
          {"key": {"type": "string"},
           "kind": {"type": "string", "enum": ["question", "operator_task"]}},
          ("key", "kind")),
    _tool("close_on_decision", "Router: close a tier-none item on a verified decision + hashed batch file.",
          {"key": {"type": "string"}, "batch_ref": {"type": "string"}},
          ("key", "batch_ref")),
    _tool("answer", "Router: answer a question/decision item (verified decision names it in Closes:) and close it.",
          {"key": {"type": "string"}, "text": {"type": "string"},
           "decision_id": {"type": "string"}}, ("key", "text", "decision_id")),
    _tool("set_legacy_state", "Router: move an open legacy item to claimed/in_review.",
          {"key": {"type": "string"}, "state": {"type": "string"},
           "note": {"type": "string"}}, ("key", "state")),
    _tool("waive_floor", "Router: waive the ancestry floor for one legacy item (decision D-NNN, optional sha_floor).",
          {"key": {"type": "string"}, "decision": {"type": "string"},
           "sha_floor": {"type": "string"}}, ("key", "decision")),
    _tool("apply_ops", "Router: apply a list of BATCH.md ops atomically (one transaction).",
          {"ops": {"type": "array", "items": {"type": "object"}},
           "batch_ref": {"type": "string"},
           "base_seq": {"type": "integer"},
           "dry_run": {"type": "boolean"}}, ("ops",)),
    _tool("cancel", "Cancel an active item.",
          {"key": {"type": "string"}, "reason": {"type": "string"}}, ("key",)),
    _tool("priority", "Set router-owned priority.",
          {"key": {"type": "string"}, "n": {"type": "integer"}},
          ("key", "n")),
    _tool("blocked", "Active items with unmet dependencies.",
          {"project": {"type": "string"}}),
    _tool("waves", "Layered schedule of non-terminal items."),
    _tool("critical_path", "Longest remaining path by estimate.",
          {"project": {"type": "string"}}),
    _tool("percent", "Percent complete.",
          {"by": {"type": "string"}, "project": {"type": "string"}}),
    _tool("needs_you", "Items waiting on the operator (operator flag or question/decision/operator_task), most-blocking first.",
          {"project": {"type": "string"}}),
    _tool("answers", "Answered question/decision items, newest first, with superseded_by when corrected.",
          {"project": {"type": "string"}}),
    _tool("audit_meta", "Router/l1: items missing description/estimate/done criteria/owner, plus claims quiet for stale_days days.",
          {"project": {"type": "string"}, "stale_days": {"type": "integer"}}),
    _tool("alerts", "Leases, overdue reviews, findings, refusals.",
          {"project": {"type": "string"}}),
    _tool("digest", "Per-project counts and recent done (router)."),
    _tool("whoami", "The authenticated principal."),
    _tool("create", "Create items: l1 in own project only, router anywhere.",
          {"items": {"type": "array", "items": {"type": "object"}}},
          ("items",)),
    _tool("edit", "Edit an open item: l1 own project only, router anywhere; locked once done/cancelled.",
          {"key": {"type": "string"}, "fields": {"type": "object"}},
          ("key", "fields")),
    _tool("mint_run_token", "Router/l1: mint a run-bound token (writer|reviewer) for a claimed/in_review item; shown once.",
          {"key": {"type": "string"}, "role": {"type": "string"},
           "run_id": {"type": "string"}}, ("key", "role", "run_id")),
    _tool("record_run", "Spawner: record which models ran on a claimed item.",
          {"key": {"type": "string"}, "run": {"type": "object"}},
          ("key", "run")),
    _tool("fleet_msg_send", "Bus: signed write {to, text}; queued in the spool if the peer is down.",
          {"to": {"type": "string"}, "text": {"type": "string"}},
          ("to", "text")),
    _tool("fleet_msg_read", "Bus: message rows for this host; since is a sequence or a timestamp.",
          {"since": {"type": "string"}}),
)

#: name -> (HTTP method, /v1/<verb>).  All other args pass through.
_ROUTES = {
    "ready": ("GET", "ready"), "show": ("GET", "show"),
    "claim": ("POST", "claim"), "delegate": ("POST", "delegate"),
    "heartbeat": ("POST", "heartbeat"), "release": ("POST", "release"),
    "evidence": ("POST", "add_evidence"), "submit": ("POST", "submit"),
    "verdict": ("POST", "verdict"), "cancel": ("POST", "cancel"),
    "waive_second_family": ("POST", "waive_second_family"),
    "close_on_decision": ("POST", "close_on_decision"),
    "withdraw_review": ("POST", "withdraw_review"),
    "reopen": ("POST", "reopen"),
    "set_kind": ("POST", "set_kind"),
    "requeue": ("POST", "requeue"),
    "answer": ("POST", "answer"),
    "set_legacy_state": ("POST", "set_legacy_state"),
    "waive_floor": ("POST", "waive_floor"),
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
