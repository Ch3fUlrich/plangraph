"""Hermetic argv contracts for the lean Claude review harness."""

import json
import sys
from pathlib import Path

import pytest

from plangraph.routing_support.claude_seat import (
    REVIEW_DENIED,
    REVIEW_TOOLS,
    claude_access_args,
)
from plangraph.routing_support.clients import build_command


@pytest.mark.parametrize("role", ["review", "implement", "orchestrate"])
@pytest.mark.parametrize("mode", ["tools", "inline-only", "prompt-only"])
def test_role_mode_argv(role, mode, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = build_command("p", "claude/test", role=role, client="claude",
                        tool_mode=mode)
    prefix = ["claude", "-p", "--model", "claude/test", "--output-format", "stream-json", "--verbose"]
    args = claude_access_args(role, mode)
    assert cmd == prefix + args
    if role != "review":
        assert args == ["--permission-mode", "plan"]  # V5: exact old argv
        return
    # V1: permission strings remain exactly the old layer, never broadened.
    assert args[:8] == ["--permission-mode", "default", "--setting-sources", "user",
                        "--allowedTools", REVIEW_TOOLS, "--disallowedTools", REVIEW_DENIED]
    assert args[args.index("--tools") + 1] == ("" if mode == "prompt-only" else "Read,Grep,Glob,Bash")
    assert "--strict-mcp-config" in args
    assert "--disable-slash-commands" in args
    config = Path(args[args.index("--mcp-config") + 1])
    assert config.is_absolute() and config.is_file()
    assert json.loads(config.read_text()) == {"mcpServers": {}}


@pytest.mark.parametrize("client", [None, "claude"])
@pytest.mark.parametrize("known", [True, False])
def test_clients_derives_leg_mode(monkeypatch, client, known):
    from dataclasses import replace

    from plangraph.planner import legs

    leg = replace(legs.DEFAULT_LEGS[-1], model="claude/test", tool_mode="prompt-only")
    monkeypatch.setattr(legs, "DEFAULT_LEGS", (leg,) if known else ())
    cmd = build_command("p", "claude/test", role="review", client=client,
                        gateway=False)
    assert cmd[cmd.index("--tools") + 1] == ("" if known else "Read,Grep,Glob,Bash")


def test_clients_explicit_mode_overrides_leg(monkeypatch):
    from dataclasses import replace

    from plangraph.planner import legs

    monkeypatch.setattr(legs, "DEFAULT_LEGS", (
        replace(legs.DEFAULT_LEGS[-1], tool_mode="inline-only"),))
    cmd = build_command("p", "claude-haiku-5-5", role="review", client="claude",
                        tool_mode="tools")
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob,Bash"


def test_gateway_prompt_only(monkeypatch, tmp_path):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"providers":{"fixture":{}}}')
    cmd = build_command("/not-a-command", "test/unregistered", role="review",
                        client="opencode", tool_mode="prompt-only")
    assert cmd[0] == sys.executable
    config = json.loads(Path(cmd[2]).read_text())
    assert cmd[3:] == ["opencode", "run", "--standalone", "--model",
                       "test/unregistered", "--agent", "lean-review"]
    agent = config["agents"]["lean-review"]
    assert agent["steps"] == 1  # OpenCode final step removes tools entirely.
    assert agent["permissions"] == [{"action": "*", "resource": "*", "effect": "deny"}]
    assert {p["action"] for p in config["policies"]} == {
        "tool.use", "mcp.use", "skill.use", "plugin.use"}
    assert all(p["effect"] == "deny" and p["resource"] == "*" for p in config["policies"])
    assert config["providers"] == {"fixture": {}}
    assert "/not-a-command" not in cmd  # prompt goes over stdin, not slash dispatch


def test_package_resource():
    import tomllib
    from importlib.resources import files

    assert json.loads(files("plangraph").joinpath("config/empty-mcp.json").read_text()) == {"mcpServers": {}}
    packaging = tomllib.loads(Path("pyproject.toml").read_text())
    assert "config/*.json" in packaging["tool"]["setuptools"]["package-data"]["plangraph"]


def test_unknown_model_tools(monkeypatch):
    from plangraph.routing_support import clients

    def reject(_):
        raise ValueError("unregistered")
    monkeypatch.setattr(clients, "detect_client_for_model", reject)
    assert build_command("p", "legacy-alias", role="review") == [
        "opencode", "run", "--standalone", "--model", "legacy-alias"]


def test_default_review_modes():
    from plangraph.planner.legs import DEFAULT_LEGS

    assert any(leg.tool_mode == "inline-only" for leg in DEFAULT_LEGS)
    leg = next(leg for leg in DEFAULT_LEGS if leg.harness == "claude")
    cmd = build_command("p", leg.model, client="claude", role="review")
    assert cmd[cmd.index("--tools") + 1] == ""
