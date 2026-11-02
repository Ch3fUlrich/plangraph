"""D-3605/D-3619: Claude Haiku review fallback leg over the Claude subscription (hermetic)."""
from __future__ import annotations

import json

from plangraph.planner.legs import DEFAULT_LEGS, HARNESSES, load_legs
from plangraph.planner.routing import plan_spawn
from plangraph.routing_support.clients import build_child_env, build_command, STDIN_PROMPT_HARNESSES
from plangraph.spawner import runner as runner_mod

from registry_route_util import CARD, live_policy  # noqa: F401


def _clear(model, **kwargs):
    return {"state": "available", "evidence": None}


def _leg():
    return next(l for l in DEFAULT_LEGS if l.id == "claude-haiku-5.5")


def test_leg_present_review_only_and_last():
    leg = _leg()
    assert leg is DEFAULT_LEGS[-1]
    assert leg.roles == ("review",) and leg.harness == "claude" and leg.claude_family
    assert leg.model == "claude-haiku-5-5" and not leg.expires
    assert "claude" in HARNESSES


def test_review_route_ends_with_haiku_for_mimo_writer():
    plan = plan_spawn(CARD, role="review", writer_families=("mimo",), leg_reader=_clear)
    ids = [l.id for l in plan.legs]
    assert ids[-1] == "claude-haiku-5.5"
    assert ids.index("claude-haiku-5.5") > max(
        i for i, l in enumerate(plan.legs) if not l.claude_family)


def test_claude_or_anthropic_writer_never_gets_haiku():
    for writers in (("anthropic",), ("claude",), ("claude", "mimo"), ("Claude",), ("ANTHROPIC",),
                    ("Claude-Sonnet",), ("claude-sonnet-5-5",), ("anthropic/claude-haiku-5-5",),
                    ("console-anthropic/claude-sonnet-5-5",), ("Anthropic/Claude", "mimo")):
        plan = plan_spawn(CARD, role="review", writer_families=writers, leg_reader=_clear)
        assert "claude-haiku-5.5" not in [l.id for l in plan.legs]


def test_argv_has_model_stdin_prompt_and_no_api_key():
    cmd = build_command("secret prompt", "claude-haiku-5-5", client="claude")
    assert cmd[:2] == ["claude", "-p"] and "claude-haiku-5-5" in cmd
    assert "stream-json" in cmd and "secret prompt" not in cmd
    at = cmd.index("--permission-mode")
    assert cmd[at:at + 2] == ["--permission-mode", "plan"]
    assert "claude" in STDIN_PROMPT_HARNESSES


def test_harnesses_validation_accepts_claude(tmp_path):
    path = tmp_path / "legs.json"
    path.write_text(json.dumps({"legs": [{
        "id": "c", "tier": "subscription", "harness": "claude", "model": "claude-haiku-5-5",
        "family": "claude", "windows": ["128k"], "max_request_tokens": 1000,
        "trains_on_data": False}]}))
    assert [l.harness for l in load_legs(path)] == ["claude"]


def test_witness_reads_stream_json_model_and_env_scrub_names():
    assert runner_mod._claude_stream_model('x\n{"type":"system","model":"m1"}') == "m1"
    assert runner_mod._claude_stream_model("not json") is None


def test_claude_child_env_drops_credentials_and_routing_keeps_login(monkeypatch):
    dropped = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
               "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "AWS_BEARER_TOKEN_BEDROCK",
               "CLAUDE_CODE_OAUTH_TOKEN")
    for k in dropped:
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("HOME", "/h")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cfg")
    env = runner_mod.scrub_claude_env(build_child_env())
    assert not set(dropped) & set(env)
    assert env["HOME"] == "/h" and env["CLAUDE_CONFIG_DIR"] == "/cfg" and "PATH" in env


def test_anthropic_writer_excludes_every_claude_family_review_leg():
    plan = plan_spawn(CARD, role="review", writer_families=("anthropic",), leg_reader=_clear)
    assert not [l.id for l in plan.legs if l.claude_family]
