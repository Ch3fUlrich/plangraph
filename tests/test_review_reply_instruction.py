"""Antigravity Hydrogen in plan mode turns a bare request into a plan artifact; review prompts tell it to answer in the reply."""
from plangraph.routing_support.clients import build_standalone_fallback_command
from plangraph.spawner import leg_probe
from plangraph.spawner.prompt_input import header

INSTRUCTION = 'Answer in plain text in this reply only; do not create a plan file'


def test_review_probe_prompt_asks_for_the_reply():
    assert INSTRUCTION in leg_probe.REVIEW_PROMPT


def test_review_role_header_asks_for_the_reply_for_every_agent():
    for harness in ('agy', 'codex', 'opencode', 'claude'):
        assert INSTRUCTION in header(harness, 'review')


def test_implement_header_has_no_reply_only_instruction():
    assert INSTRUCTION not in header('agy', 'implement')


def test_hydrogen_review_launch_still_selects_plan_mode():
    cmd = build_standalone_fallback_command('agy', '', model='gemini-4-hydrogen-high', role='review')
    assert cmd[1:3] == ['--mode', 'plan']
