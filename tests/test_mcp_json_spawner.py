import json
import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_project_mcp_json_declares_spawner():
    text = (REPO / ".mcp.json").read_text(encoding="utf-8")
    servers = json.loads(text)["mcpServers"]
    assert set(servers) == {"omnigraph", "plangraph-spawner"}
    spawner = servers["plangraph-spawner"]
    assert spawner["type"] == "stdio"
    assert spawner["command"] == "sh"
    assert spawner["env"]["PYTHONPATH"] == "src"
    assert spawner["args"][0] == "-c"
    args_text = " ".join(spawner["args"])
    assert "omniroute-mgmt.key" in args_text
    assert "plangraph.spawner.mcp" in args_text
    assert (REPO / "src" / "plangraph" / "spawner" / "mcp.py").is_file()


def test_project_mcp_json_has_no_embedded_tokens():
    text = (REPO / ".mcp.json").read_text(encoding="utf-8")
    assert "bearer" not in text.lower()
    assert re.search(r"[A-Za-z0-9+/_-]{32,}", text) is None
