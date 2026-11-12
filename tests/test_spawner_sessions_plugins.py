"""Hermetic checks for fleet-only plugin settings; no launches or user config."""
import json

import pytest

from plangraph.spawner.sessions_agents import _AgentsMixin


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_fleet_disables_superpowers_only_in_launch_argv(platform, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    attended_settings = '{"enabledPlugins":{"superpowers@superpowers-marketplace":true}}'
    settings.write_text(attended_settings)
    builder = _AgentsMixin()
    builder._platform = platform
    builder._which = lambda _: "C:/bin/claude.exe"
    # The ordinary attended invocation uses the executable without fleet flags.
    attended_argv = builder._claude_head()
    expected_head = ["C:/bin/claude.exe"] if platform == "win32" else ["claude"]
    argv, _ = builder._build_argv(
        "fleet", tmp_path, "model", "high", "default", tmp_path / "mcp.json",
        "style", "prompt", {},
    )
    assert argv.count("--settings") == 1
    index = argv.index("--settings")
    assert json.loads(argv[index + 1]) == {
        "enabledPlugins": {"superpowers@superpowers-marketplace": False},
    }
    assert argv[:2] == expected_head + ["--bg"]
    assert argv[-3:] == ["--append-system-prompt", "style", "prompt"]
    assert attended_argv == builder._claude_head() == expected_head
    assert settings.read_text() == attended_settings
