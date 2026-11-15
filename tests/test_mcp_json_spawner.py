import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_project_mcp_json_declares_spawner():
    text = (REPO / ".mcp.json").read_text(encoding="utf-8")
    servers = json.loads(text)["mcpServers"]
    assert set(servers) == {"omnigraph", "plangraph-spawner"}
    spawner = servers["plangraph-spawner"]
    assert spawner["type"] == "stdio" and spawner["command"] == "sh"
    assert spawner["env"] == {"PYTHONPATH": "src"}
    assert spawner["args"][0] == "-c"
    args_text = " ".join(spawner["args"])
    assert "omniroute-mgmt.key" in args_text
    assert "omniroute-base-url" in args_text and "tr -d" in args_text
    assert "plangraph.spawner.mcp" in args_text
    assert (REPO / "src" / "plangraph" / "spawner" / "mcp.py").is_file()


def test_project_mcp_json_has_no_embedded_tokens():
    text = (REPO / ".mcp.json").read_text(encoding="utf-8")
    assert "bearer" not in text.lower()
    assert re.search(r"[A-Za-z0-9+/_-]{32,}", text) is None


def test_project_mcp_spawner_changelog_is_one_bullet():
    lines = (REPO / "changelog.d/MCP-SPAWNER-PROJECT-SERVER.md").read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith("- ")


def _probe_spawner(tmp_path, key_bytes=b"stub-key", url_bytes=None, caller_url="http://caller.invalid/v1"):
    spawner = json.loads((REPO / ".mcp.json").read_text())['mcpServers']['plangraph-spawner']
    key = tmp_path / ".config" / "plangraph" / "omniroute-mgmt.key"
    key.parent.mkdir(parents=True)
    if key_bytes is not None:
        key.write_bytes(key_bytes)
    if url_bytes is not None:
        key.with_name("omniroute-base-url").write_bytes(url_bytes)
    probe = tmp_path / "bin" / "python3"
    probe.parent.mkdir()
    probe.write_text(
        '#!/bin/sh\n[ "$*" = "-m plangraph.spawner.mcp" ] || exit 2\n'
        f'[ "$PLANGRAPH_GATEWAY_LOG_TOKEN" = "{"stub-key" if key_bytes is not None else ""}" ] || exit 3\n'
        'printf "%s" "${OMNIROUTE_BASE_URL-}"\n'
    )
    probe.chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": f"{probe.parent}:/usr/bin:/bin", **spawner["env"]}
    if caller_url is not None:
        env["OMNIROUTE_BASE_URL"] = caller_url
    return subprocess.run([spawner["command"], *spawner["args"]], env=env, capture_output=True, timeout=5)


@pytest.mark.parametrize("key_bytes", [b"stub-key" + ending for ending in (b"", b"\n", b"\r", b"\r\n")]
                         + [b"stub-\r\nkey\r\n", None], ids=["plain", "lf", "cr", "crlf", "embedded-crlf", "absent-key"])
def test_project_mcp_spawner_strips_key_line_endings(tmp_path, key_bytes):
    result = _probe_spawner(tmp_path, key_bytes=key_bytes)
    assert result.returncode == 0
    key = tmp_path / ".config" / "plangraph" / "omniroute-mgmt.key"
    assert result.stderr == (f"plangraph spawner: gateway log key unreadable: {key}\n".encode() if key_bytes is None else b"")


@pytest.mark.parametrize("url_bytes", [b"http://gateway.invalid/v1" + ending for ending in (b"", b"\n", b"\r", b"\r\n")]
                         + [b"http://gateway.\r\ninvalid/v1\r\n", b"", b"\r\n", None], ids=["plain", "lf", "cr", "crlf", "embedded-crlf", "empty", "only-crlf", "absent"])
@pytest.mark.parametrize("caller_url", ["http://caller.invalid/v1", None, ""])
def test_project_mcp_spawner_loads_local_gateway_or_inherits_caller(tmp_path, url_bytes, caller_url):
    result = _probe_spawner(tmp_path, url_bytes=url_bytes, caller_url=caller_url)
    expected = (url_bytes or b"").decode().replace("\r", "").replace("\n", "") or caller_url or ""
    assert result.returncode == 0
    assert result.stdout.decode() == expected
    u = tmp_path / ".config" / "plangraph" / "omniroute-base-url"
    warning = f"plangraph spawner: gateway base URL unset: {u}\n".encode()
    assert result.stderr == (warning if not expected else b"")


def test_project_mcp_spawner_has_no_literal_gateway_port():
    for path in (REPO / ".mcp.json", Path(__file__), REPO / "changelog.d/MCP-SPAWNER-PROJECT-SERVER.md"):
        assert re.search(r'://[^\s"]+:\d+', path.read_text()) is None
