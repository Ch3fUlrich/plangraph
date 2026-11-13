"""D-3605/D-3619: Claude Haiku review fallback leg over the Claude subscription (hermetic).

The claude child env allowlist and the harness_missing admission live in test_claude_child_env.py (D-3649).
"""
from __future__ import annotations

import json

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, HARNESSES, load_legs
from plangraph.planner.routing import plan_spawn
from plangraph.routing_support.clients import build_command, build_standalone_fallback_command, STDIN_PROMPT_HARNESSES
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
    assert leg.model == "claude-haiku-5-5" and leg.expires is None
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


@pytest.mark.parametrize("online", [True, False])
def test_review_seat_gets_pytest_only_and_no_plan_mode(online):
    cmd = build_command("p", "claude-haiku-5-5", role="review", client="claude", gateway_online=online)
    assert cmd[cmd.index("--permission-mode") + 1] == "default"
    tools = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert tools == ["Read", "Grep", "Glob", "Bash(python3 -m pytest -q *)"]
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert {"Write", "Edit", "Bash(python3 -m pytest -q -*)", "Bash(python3 -m pytest -q * -*)"} <= set(denied)
    assert cmd[cmd.index("--setting-sources") + 1] == "user"   # the diff under review cannot widen its seat
    assert "bypassPermissions" not in cmd and "--dangerously-skip-permissions" not in cmd


def test_offline_review_fallback_keeps_the_scope_and_denies_paths_out():
    cmd = build_standalone_fallback_command("claude", "p", "claude-haiku-5-5", role="review")
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert {"Bash(*$*)", "Bash(*`*)", "Bash(*..*)", "Bash(python3 -m pytest -q /*)"} <= set(denied)


def test_non_review_role_stays_in_plan_mode_without_tools():
    cmd = build_command("p", "claude-haiku-5-5", role="implement", client="claude")
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"
    assert "--allowedTools" not in cmd


def test_harnesses_validation_accepts_claude(tmp_path):
    path = tmp_path / "legs.json"
    path.write_text(json.dumps({"legs": [{
        "id": "c", "tier": "subscription", "harness": "claude", "model": "claude-haiku-5-5",
        "family": "claude", "windows": ["128k"], "max_request_tokens": 1000,
        "trains_on_data": False}]}))
    assert [l.harness for l in load_legs(path)] == ["claude"]


def test_witness_reads_stream_json_model():
    assert runner_mod._claude_stream_model('x\n{"type":"system","model":"m1"}') == "m1"
    assert runner_mod._claude_stream_model("not json") is None


def test_anthropic_writer_excludes_every_claude_family_review_leg():
    plan = plan_spawn(CARD, role="review", writer_families=("anthropic",), leg_reader=_clear)
    assert not [l.id for l in plan.legs if l.claude_family]


@pytest.mark.parametrize('writers', [('claude','gpt'),('gpt','anthropic')])
def test_explicit_haiku_cannot_review_any_claude_writer_part(tmp_path, monkeypatch, writers):
    import asyncio
    from plangraph.planner.routing import SpawnRefused
    from plangraph.spawner.service import SpawnService
    service = SpawnService(state=tmp_path, leg_reader=_clear)
    with pytest.raises(SpawnRefused, match='excluded writer family'):
        asyncio.run(service.spawn(card=CARD, role='review', writer_families=writers,
            leg='claude-haiku-5.5', override_reason='hermetic explicit review', dry_run=True))


def test_explicit_tool_mode_cannot_enable_haiku_tools():
    cmd = build_command('p','claude-haiku-5-5',role='review',client='claude',tool_mode='tools')
    assert cmd[cmd.index('--tools')+1] == ''


def test_explicit_haiku_uses_normal_run_record_and_stdout(tmp_path):
    import asyncio
    from test_spawner_paid_last import fake_runner
    from plangraph.spawner.service import SpawnService
    runner = fake_runner(tmp_path,text='VERDICT: PASS')
    service = SpawnService(runner=runner,state=tmp_path,leg_reader=_clear)
    result = asyncio.run(service.spawn(card=CARD,prompt='review this',role='review',
        writer_families=('gpt',),leg='claude-haiku-5.5',
        override_reason='hermetic Haiku review',wait_seconds=None))
    record = service.result(result['run_id'])
    assert runner.calls[0]['client'] == 'claude'
    assert runner.calls[0]['model_or_combo'] == 'claude-haiku-5-5'
    assert record['chosen_leg']['id'] == 'claude-haiku-5.5'
    assert record['stdout_tail'] == 'VERDICT: PASS'
    assert service._path(result['run_id']).is_file()
    assert record['attempts'][0]['leg']['id'] == 'claude-haiku-5.5'


def test_catalog_claude_override_cannot_bypass_mixed_writer_fence(tmp_path, monkeypatch):
    import asyncio
    from plangraph.planner.routing import SpawnRefused
    from plangraph.spawner.service import SpawnService
    model = 'anthropic/claude-haiku-catalog'
    monkeypatch.setattr('plangraph.spawner.resolver.get_catalog_models', lambda: [
        dict(id=model,tier='subscription',harness='claude',context_tokens=200000,
             tool_mode='prompt-only',active=True,available=1,trains_on_prompts=False)])
    service = SpawnService(state=tmp_path,leg_reader=_clear)
    with pytest.raises(SpawnRefused,match='excluded writer family'):
        asyncio.run(service.spawn(card=CARD,role='review',writer_families=('claude','gpt'),
            model=model,override_reason='hermetic catalog review',dry_run=True))
