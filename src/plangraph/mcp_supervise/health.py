"""Health probe for MCP streamable-HTTP endpoints."""

import json
import urllib.error
import urllib.request
from typing import Any, Optional


def probe(url: str, timeout: float = 5.0) -> bool:
    """Send an MCP 'initialize' JSON-RPC POST request to url.

    Returns True if the response is a valid JSON-RPC 2.0 result, False otherwise.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "mcp-supervise-probe",
                "version": "1.0.0",
            },
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 202):
                return False
            raw = resp.read().decode("utf-8", errors="replace")
            return _is_valid_jsonrpc_result(raw)
    except Exception:
        return False


def _is_valid_jsonrpc_result(raw: str) -> bool:
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

    if not isinstance(data, dict):
        return False
    if data.get("jsonrpc") != "2.0":
        return False
    if "error" in data:
        return False
    return "result" in data
