"""Antigravity Hydrogen in plan mode turns a bare request into a plan artifact; review prompts tell it to answer in the reply."""
from pathlib import Path

from plangraph.routing_support.clients import build_standalone_fallback_command
from plangraph.spawner import leg_probe
from plangraph.spawner.prompt_input import compose, header

INSTRUCTION = 'Answer in plain text in this reply only; do not create a plan file'
ROOT = str(Path(__file__).resolve().parents[1])
HYDROGEN = 'agy-gemini-4-hydrogen-high'


def test_review_probe_prompt_asks_for_the_reply():
    assert 'Reply with the single word OK.' in leg_probe.REVIEW_PROMPT
    assert 'Do not create a plan or any other file.' in leg_probe.REVIEW_PROMPT


def test_review_role_header_asks_for_the_reply_for_every_agent():
    for harness in ('agy', 'codex', 'opencode', 'claude'):
        assert INSTRUCTION in header(harness, 'review')


def test_implement_header_has_no_reply_only_instruction():
    assert INSTRUCTION not in header('agy', 'implement')


def test_composed_hydrogen_review_prompt_carries_the_instruction():
    text = compose('Review the change of this batch.', 'review', 'agy', ROOT, HYDROGEN)
    assert INSTRUCTION in text.split('\n\n', 1)[0]


def test_composed_hydrogen_probe_prompt_carries_the_instruction():
    text = compose(leg_probe.REVIEW_PROMPT, 'review', 'agy', ROOT, HYDROGEN)
    assert INSTRUCTION in text.split('\n\n', 1)[0]
    assert text.rstrip().endswith(leg_probe.REVIEW_PROMPT)


def test_hydrogen_review_launch_still_selects_plan_mode():
    prompt = compose('Review the change of this batch.', 'review', 'agy', ROOT, HYDROGEN)
    cmd = build_standalone_fallback_command('agy', prompt, model='gemini-4-hydrogen-high', role='review')
    assert cmd[1:3] == ['--mode', 'plan']
