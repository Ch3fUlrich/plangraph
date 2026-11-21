"""A toolless inline review seat gets a prompt it can answer honestly, and always a verdict."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.review import verdict
from plangraph.spawner import prompt_input
from plangraph.spawner.seat_spawn import SeatSpawnMixin

SKILL = (Path(__file__).resolve().parents[1] / 'skills/review/SKILL.md').read_text(encoding='utf-8')


@pytest.fixture
def working_tree_skill(monkeypatch):
    monkeypatch.setattr(prompt_input, 'read_skill', lambda name, cwd: SKILL if name == 'review' else None)


def test_inline_prompt_has_the_inline_rules_and_no_tool_run_demand_with_the_real_skill_loader():
    # Whatever skill text origin/main serves, an inline seat's prompt holds the inline rules only.
    text = prompt_input.compose('Review the change', 'review', 'opencode', None, 'mimo-v2.6-pro',
                                tool_mode='inline-only')
    for rule in ('tool mode inline', 'no tools', 'quoted diff line', 'FILES READ: diff only', 'VERDICT: ACCEPT'):
        assert rule in text, rule
    for demand in ('Run tests', 'run tests', 'Run only the tests', 'run the tests', 'You may read the repository'):
        assert demand not in text, demand
    assert text.endswith('Review the change')
    assert SKILL not in text


def test_tools_prompt_keeps_the_full_skill(working_tree_skill):
    text = prompt_input.compose('Review the change', 'review', 'opencode', None, 'mimo-v2.6-pro')
    assert text.count(SKILL) == 1 and '## Inline seats' in text


def test_tools_leg_prompt_is_unchanged(working_tree_skill):
    tools = prompt_input.compose('Review the change', 'review', 'opencode', None, 'mimo-v2.6-pro')
    assert tools == prompt_input.compose('Review the change', 'review', 'opencode', None, 'mimo-v2.6-pro',
                                         tool_mode='tools')
    assert tools.split('\n\n', 1)[0].endswith('Run tests, never edit files, commit, push or change git config.')
    assert prompt_input.compose('Do it', 'implement', 'claude', None, tool_mode='inline-only').split(
        '\n\n', 1)[0] == prompt_input.header('claude', 'implement')


def test_composing_an_inline_prompt_twice_adds_nothing(working_tree_skill):
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


def test_a_brief_that_starts_with_the_review_skill_loses_it_on_an_inline_seat():
    brief = SKILL + '\nReview the change'
    inline = prompt_input.compose(brief, 'review', 'opencode', None, 'mimo-v2.6-pro', tool_mode='inline-only')
    assert SKILL not in inline and 'Run only the tests' not in inline and 'run tests' not in inline.lower()
    assert inline.endswith('Review the change') and 'tool mode inline' in inline
    # Tools legs keep it exactly once.
    assert prompt_input.compose(brief, 'review', 'opencode', None, 'mimo-v2.6-pro').count(SKILL) == 1


TOOLS_BLOCK = ('[tools-only]Your working directory is a disposable checkout. Open callers, tests '
               'and docs to verify each finding; run read-only commands and the tests a finding depends on; never edit.'
               '[/tools-only]\n\n')
ITEMS = ('Writer brief:\nthe item says "[tools-only]keep this[/tools-only]" and quotes a finding: '
         '[tools-only]run the tests[/tools-only]\nThe diff is base..head.\nName each finding as file:line.\n')
NATIVE_BRIEF = TOOLS_BLOCK + ITEMS


def test_native_review_brief_loses_its_tool_instructions_on_an_inline_seat():
    inline = prompt_input.compose(SKILL + '\n' + NATIVE_BRIEF, 'review', 'opencode', None, 'mimo-v2.6-pro',
                                  tool_mode='inline-only')
    for text in ('Open callers', 'run read-only commands', 'disposable checkout'):
        assert text not in inline, text
    # Marker text inside the items is the brief's own and stays.
    assert '"[tools-only]keep this[/tools-only]"' in inline and '[tools-only]run the tests[/tools-only]' in inline
    assert 'The diff is base..head.' in inline and inline.count('[tools-only]') == 2


def test_native_review_brief_keeps_its_tool_instructions_without_markers_on_a_tools_leg(working_tree_skill):
    tools = prompt_input.compose(SKILL + '\n' + NATIVE_BRIEF, 'review', 'opencode', None, 'mimo-v2.6-pro')
    assert 'Open callers, tests and docs' in tools and 'never edit.' in tools
    assert tools.count('[tools-only]') == 2 and tools.count(SKILL) == 1
    assert tools.index('Open callers') < tools.index('Writer brief:')
    # The marker block is only interpreted for a review prompt.
    implement = prompt_input.compose(NATIVE_BRIEF, 'implement', 'claude', None, tool_mode='inline-only')
    assert implement.count('[tools-only]') == 3


def test_a_skill_version_the_loader_does_not_know_is_still_recognised_by_its_front_matter():
    # The writer embedded another version of the review skill than origin/main or this tree serve.
    other = SKILL.replace('Run only the tests', 'Run just the tests of the touched crates').replace(
        'Independence', 'Independence of the reviewer')
    assert other != SKILL
    brief = other + '\n' + NATIVE_BRIEF
    inline = prompt_input.compose(brief, 'review', 'opencode', None, 'mimo-v2.6-pro', tool_mode='inline-only')
    for text in ('Open callers', 'run read-only commands', 'Run just the tests', 'Independence of the reviewer'):
        assert text not in inline, text
    assert 'The diff is base..head.' in inline and 'tool mode inline' in inline


def test_a_quoted_marker_without_a_block_after_the_skill_is_left_alone():
    brief = SKILL + '\nWriter brief (item keys and Done-when lines):\nitem: "[tools-only]x[/tools-only]"\n'
    inline = prompt_input.compose(brief, 'review', 'opencode', None, 'mimo-v2.6-pro', tool_mode='inline-only')
    assert '"[tools-only]x[/tools-only]"' in inline and 'Writer brief' in inline
