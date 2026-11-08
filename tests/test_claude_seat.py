"""Hermetic argv contracts for the lean Claude review harness."""

import json
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
@pytest.mark.parametrize("online", [True, False])
def test_role_mode_argv(role, mode, online, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = build_command("p", "claude/test", role=role, client="claude",
                        tool_mode=mode, gateway_online=online)
    prefix = ["claude", "-p", "--model", "claude/test", "--output-format", "stream-json", "--verbose"]
    args = claude_access_args(role, mode)
    assert cmd == prefix + args
    if role != "review":
        assert args == ["--permission-mode", "plan"]  # V5: exact old argv
        return
    # V1: permission strings remain exactly the old layer, never broadened.
    assert args[:8] == ["--permission-mode", "default", "--setting-sources", "user",
                        "--allowedTools", REVIEW_TOOLS, "--disallowedTools", REVIEW_DENIED]
    assert args[args.index("--tools") + 1] == ("Read,Grep,Glob,Bash" if mode == "tools" else "")
    assert "--strict-mcp-config" in args
    assert "--disable-slash-commands" in args
    config = Path(args[args.index("--mcp-config") + 1])
    assert config.is_absolute() and config.is_file()
    assert json.loads(config.read_text()) == {"mcpServers": {}}


@pytest.mark.parametrize("client", [None, "claude"])
@pytest.mark.parametrize("online", [True, False])
@pytest.mark.parametrize("known", [True, False])
def test_clients_derives_leg_mode(monkeypatch, client, online, known):
    from dataclasses import replace

    from plangraph.planner import legs

    leg = replace(legs.DEFAULT_LEGS[-1], model="claude/test", tool_mode="inline-only")
    monkeypatch.setattr(legs, "DEFAULT_LEGS", (leg,) if known else ())
    cmd = build_command("p", "claude/test", role="review", client=client,
                        gateway=False, gateway_online=online)
    assert cmd[cmd.index("--tools") + 1] == ("" if known else "Read,Grep,Glob,Bash")


def test_clients_explicit_mode_overrides_leg(monkeypatch):
    from dataclasses import replace

    from plangraph.planner import legs

    monkeypatch.setattr(legs, "DEFAULT_LEGS", (
        replace(legs.DEFAULT_LEGS[-1], tool_mode="inline-only"),))
    cmd = build_command("p", "claude-haiku-5-5", role="review", client="claude",
                        tool_mode="tools")
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob,Bash"
