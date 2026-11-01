"""Health probe for MCP streamable-HTTP endpoints."""

import json
import urllib.request
from typing import Any, Dict, Optional

METHOD_NOT_FOUND = -32601

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "mcp-supervise-probe", "version": "1.0.0"},
    },
}
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def probe(url: str, timeout: float = 5.0, deep: bool = True) -> bool:
    """True if `url` answers an MCP `initialize` with a valid JSON-RPC result.

    mcp-proxy answers `initialize` itself and keeps answering it after the stdio
    server behind it died (measured, 0.13.0), so `deep` also sends `tools/list`,
    which the proxy forwards: a result, or "method not found" from a server that
    has no tools, means the server behind the proxy is alive; any other error is not.
    Stateless mode forwards both to the one shared stdio child, so a probe does not
    start a new server session.
    """
    first = _post(url, _INITIALIZE, timeout)
    if first is None or not _is_result(first):
        return False
    if not deep:
        return True
    second = _post(url, _TOOLS_LIST, timeout)
    if second is None:
        return False
    if _is_result(second):
        return True
    error = second.get("error")
    return isinstance(error, dict) and error.get("code") == METHOD_NOT_FOUND


def _post(url: str, payload: Dict[str, Any], timeout: float) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 202):
                return None
            return _parse_jsonrpc(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _parse_jsonrpc(raw: str) -> Optional[Dict[str, Any]]:
    data: Optional[Any] = None
    try:
        data = json.loads(raw)
    except Exception:
        # Check for SSE-wrapped JSON-RPC
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                    break
                except Exception:
                    continue
    if isinstance(data, dict) and data.get("jsonrpc") == "2.0":
        return data
    return None


def _is_result(data: Dict[str, Any]) -> bool:
    return "error" not in data and "result" in data
