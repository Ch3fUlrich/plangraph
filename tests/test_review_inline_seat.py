"""A toolless inline review seat gets a prompt it can answer honestly, and always a verdict."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.review import verdict
from plangraph.spawner import prompt_input
from plangraph.spawner.seat_spawn import SeatSpawnMixin

SKILL = (Path(__file__).resolve().parents[1] / 'skills/review/SKILL.md').read_text(encoding='utf-8')


@pytest.fixture(autouse=True)
def working_tree_skill(monkeypatch):
    monkeypatch.setattr(prompt_input, 'read_skill', lambda name, cwd: SKILL if name == 'review' else None)


def test_inline_leg_prompt_names_its_tool_mode_and_carries_the_inline_section():
    text = prompt_input.compose('Review the change', 'review', 'opencode', None, 'mimo-v2.6-pro',
                                tool_mode='inline-only')
    first = text.split('\n\n', 1)[0]
    assert 'tool mode inline' in first and 'no tools' in first
    assert 'Run tests' not in first and 'run tests' not in first
    assert '## Inline seats' in text and text.count(SKILL) == 1
    assert 'VERDICT' in text.split('## Inline seats', 1)[1]


def test_tools_leg_prompt_is_unchanged():
    tools = prompt_input.compose('Review the change', 'review', 'opencode', None, 'mimo-v2.6-pro')
    assert tools == prompt_input.compose('Review the change', 'review', 'opencode', None, 'mimo-v2.6-pro',
                                         tool_mode='tools')
    assert tools.split('\n\n', 1)[0].endswith('Run tests, never edit files, commit, push or change git config.')
    assert prompt_input.compose('Do it', 'implement', 'claude', None, tool_mode='inline-only').split(
        '\n\n', 1)[0] == prompt_input.header('claude', 'implement')


def test_composing_an_inline_prompt_twice_adds_nothing():
    once = prompt_input.compose('Review', 'review', 'opencode', None, 'mimo-v2.6-pro', tool_mode='inline-only')
    assert prompt_input.compose(once, 'review', 'opencode', None, 'mimo-v2.6-pro', tool_mode='inline-only') == once


def test_inline_seat_reply_with_quoted_diff_findings_parses_to_a_verdict():
    head = 'a' * 40
    reply = (f'MED src/x.py:12 - `+    return items[0]` indexes a possibly empty list - guard it\n'
             f'Evidence: quoted diff line above; CI evidence named in the brief, no test run here.\n'
             f'FILES READ: diff only\nHEAD: {head}\nVERDICT: REQUEST_CHANGES\n')
    judged = verdict.judge(reply, 'stop', material='diff --git a/src/x.py b/src/x.py\n+    return items[0]\n')
    assert judged.verdict == 'fix' and not judged.empty
    accept = reply.replace('MED src', 'LOW src').replace('REQUEST_CHANGES', 'ACCEPT')
    assert verdict.judge(accept, 'stop', material='x').verdict in ('pass', 'fix')


def test_oversize_inline_diff_is_refused_with_its_reason_never_elided(tmp_path):
    from registry_route_util import _repo
    from plangraph.spawner.context_pack import PromptBoundError
    repo, base, head = _repo(tmp_path)
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=str(repo))
    leg = SimpleNamespace(harness='opencode', tool_mode='inline-only', max_request_tokens=2)
    card = {'diff_ref': {'repo': str(repo), 'base': base, 'head': head}}
    with pytest.raises(PromptBoundError, match='prompt_too_large: inline_diff_oversized'):
        seat._unbounded_leg_prompt('Review', card, 'review', leg, pack=SimpleNamespace(text=''))
