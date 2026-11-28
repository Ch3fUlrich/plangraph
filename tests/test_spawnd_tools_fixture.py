"""The pg-spawnd tools_list.json fixture must match spawner.mcp.TOOLS_METADATA."""

import json
from pathlib import Path

from plangraph.spawner.mcp import TOOLS_METADATA

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "rust" / "pg-spawnd" / "src" / "tools_list.json"
)


def test_spawnd_tools_fixture_matches_metadata():
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    # The work-mode endpoint is Rust-only; retain parity for all shared tools.
    native = [tool for tool in fixture if tool["name"] == "plangraph_work_mode"]
    assert len(native) == 1
    assert [tool for tool in fixture if tool not in native] == TOOLS_METADATA
