"""Offline seat-mode and accepted OpenCode configuration contracts."""
import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from plangraph.planner import legs
from plangraph.routing_support.clients import build_command
from plangraph.routing_support.review_mode import prompt_only_command
from tools.judge_prompt import build_judge_prompt


@pytest.mark.parametrize("mode", ["tools", "inline-only", "prompt-only"])
def test_judge_prompt_derived_from_leg(mode, monkeypatch):
    from plangraph.routing_support import clients

    leg = replace(legs.DEFAULT_LEGS[-1], model="claude/test", harness="claude", tool_mode=mode)
    monkeypatch.setattr(legs, "DEFAULT_LEGS", (leg,))
    captured = {}

    def capture(client, prompt, model, **kwargs):
        captured["prompt"] = prompt
        return []

    monkeypatch.setattr(clients, "build_standalone_fallback_command", capture)
    brief = build_judge_prompt("F1", "diff", {}, {}, tool_mode="tools")
    build_command(brief, leg.model, role="review", client=leg.harness)
    assert ("No tools are available" in captured["prompt"]) == (mode == "prompt-only")


def test_prompt_only_accepted_config_schema(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"providers":{"gateway":{}}}')
    config = json.loads(prompt_only_command(["opencode", "run"])[1].split("=", 1)[1])
    schema = json.loads(Path("tests/fixtures/opencode-review-schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(config)
    invalid = json.loads(json.dumps(config))
    invalid["agents"]["lean-review"]["permissions"] = {"*": "deny"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)


def test_missing_config_preserves_discovery(monkeypatch):
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    cmd = build_command("p", "gateway/test", client="opencode", role="review", tool_mode="prompt-only")
    assert cmd == build_command("p", "gateway/test", client="opencode", role="review", tool_mode="tools")


@pytest.mark.parametrize("value", ["{bad", "null", "[]", '{"agents":[]}'])
def test_malformed_config_falls_back(monkeypatch, value):
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", value)
    cmd = ["opencode", "run", "--model", "unknown/pair"]
    assert prompt_only_command(cmd) == cmd
