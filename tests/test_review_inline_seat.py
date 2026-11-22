"""A toolless inline review seat gets a prompt it can answer honestly, and always a verdict."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.review import verdict
from plangraph.review.prompt import render_brief
from plangraph.spawner import prompt_input
from plangraph.spawner.seat_spawn import SeatSpawnMixin

SKILL = (Path(__file__).resolve().parents[1] / 'skills/review/SKILL.md').read_text(encoding='utf-8')


@pytest.fixture
def working_tree_skill(monkeypatch):
    monkeypatch.setattr(prompt_input, 'read_skill', lambda name, cwd: SKILL if name == 'review' else None)


@pytest.mark.parametrize('mode', ['inline-only', 'prompt-only'])
def test_inline_prompt_has_the_inline_rules_and_no_tool_run_demand_with_the_real_skill_loader(mode):
    # Whatever skill text origin/main serves, an inline seat's prompt holds the inline rules only.
    text = prompt_input.compose('Review the change', 'review', 'opencode', None, 'mimo-v2.6-pro',
                                tool_mode=mode)
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
    assert tools.split('\n\n', 1)[0].endswith(
        'Run tests, never edit files, commit, push or change git config. '
        'Answer in plain text in this reply only; do not create a plan file.'
    )
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
               '[/tools-only]')
ITEMS = ('Writer brief (item keys and Done-when lines):\nthe item says "[tools-only]keep this[/tools-only]" and quotes a '
         'finding: [tools-only]run the tests[/tools-only]\nThe diff is base..head.\nName each finding as file:line.\n')


def native_brief(skill=SKILL, brief=ITEMS):
    """The native writer's review prompt: the tools-only block, the skill, then the writer brief."""
    return TOOLS_BLOCK + '\n\n[review-skill]\n' + skill + '\n[/review-skill]\n\n' + brief


def test_native_review_brief_loses_its_tool_instructions_on_an_inline_seat():
    inline = prompt_input.compose(native_brief(), 'review', 'opencode', None, 'mimo-v2.6-pro', tool_mode='inline-only')
    for text in ('Open callers', 'run read-only commands', 'disposable checkout', 'Run only the tests'):
        assert text not in inline, text
    # Marker text inside the items is the brief's own and stays.
    assert '"[tools-only]keep this[/tools-only]"' in inline and '[tools-only]run the tests[/tools-only]' in inline
    assert 'The diff is base..head.' in inline and inline.count('[tools-only]') == 2


def test_native_review_brief_keeps_its_block_once_and_the_skill_once_on_a_tools_leg(working_tree_skill):
    tools = prompt_input.compose(native_brief(), 'review', 'opencode', None, 'mimo-v2.6-pro')
    assert 'Open callers, tests and docs' in tools and tools.count(SKILL) == 1
    assert tools.index('Open callers') < tools.index(SKILL[:40]) < tools.index('Writer brief')
    again = prompt_input.compose(tools, 'review', 'opencode', None, 'mimo-v2.6-pro')
    assert again == tools
    # The block is only interpreted for a review prompt.
    implement = prompt_input.compose(TOOLS_BLOCK + ITEMS, 'implement', 'claude', None, tool_mode='inline-only')
    assert implement.count('[tools-only]') == 3


def test_a_skill_version_the_loader_does_not_know_is_still_dropped_on_an_inline_seat():
    other = SKILL.replace('Run only the tests', 'Run just the tests of the touched crates').replace(
        'Independence', 'Independence of the reviewer')
    assert other != SKILL
    inline = prompt_input.compose(native_brief(other), 'review', 'opencode', None, 'mimo-v2.6-pro',
                                  tool_mode='inline-only')
    for text in ('Open callers', 'run read-only commands', 'Run just the tests', 'Independence of the reviewer'):
        assert text not in inline, text
    assert 'The diff is base..head.' in inline and 'tool mode inline' in inline


def test_quoted_markers_in_a_versioned_brief_without_a_leading_block_are_left_alone():
    brief = SKILL + '\nWriter brief (item keys and Done-when lines):\nitem key X: "[tools-only]x[/tools-only]" criteria\n'
    inline = prompt_input.compose(brief, 'review', 'opencode', None, 'mimo-v2.6-pro', tool_mode='inline-only')
    assert 'item key X: "[tools-only]x[/tools-only]" criteria' in inline
    assert prompt_input.compose(brief, 'review', 'opencode', None, 'mimo-v2.6-pro').count('item key X') == 1


@pytest.mark.parametrize('known_prefix', [True, False])
def test_native_inline_keeps_the_entire_versioned_brief(working_tree_skill, known_prefix):
    skill = SKILL if known_prefix else SKILL.replace('name: review', 'name: review\nversion: unfamiliar')
    skill += '\nRun a shell command to check the fixture.'
    context = {'item_key': 'item-key', 'done_criteria': 'done-criteria',
               'invariants': 'first line\n\nWriter brief (item keys and Done-when lines):\nquoted heading'}
    versioned = render_brief('review', context)
    prompt = native_brief(skill, versioned + ITEMS)
    inline = prompt_input.compose(prompt, 'review', 'opencode', None, tool_mode='inline-only')
    assert 'Run a shell command to check the fixture.' not in inline
    assert inline.split('\n\n', 1)[1] == versioned + ITEMS


@pytest.mark.parametrize('body', [
    "Read plans/x.md and check its claims.",
    "```text\nRead plans/x.md and check its claims.\n```",
    "=== INLINE plans/x.md ===\nRead plans/x.md and check its claims.\n=== END plans/x.md ===",
])
def test_toolless_file_request_without_material_is_refused(tmp_path, body):
    from plangraph.routing_errors import SpawnRefused
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=str(tmp_path))
    leg = SimpleNamespace(harness='claude', tool_mode='prompt-only', max_request_tokens=1000)
    with pytest.raises(SpawnRefused, match='toolless_needs_inline'):
        seat._leg_prompt(body, {}, 'review', leg, document_review=True)


def test_toolless_judge_accepts_supplied_evidence_and_cut_file_list(tmp_path):
    from tools.judge_prompt import build_judge_prompt
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=str(tmp_path))
    leg = SimpleNamespace(harness='opencode', tool_mode='inline-only', max_request_tokens=10000)
    prompt = build_judge_prompt(
        'MED src/x.rs:1 - stale value - refresh it',
        'diff --git a/src/x.rs b/src/x.rs\n--- a/src/x.rs\n+++ b/src/x.rs\n@@ -1 +1 @@\n-old\n+new\n',
        {'src/x.rs': 'new', 'plans/cut.md': None}, {})
    result = seat._leg_prompt(prompt, {}, 'judge', leg, pack=SimpleNamespace(text=''),
                              document_review=False)
    assert '## Cut files\n- plans/cut.md' in result
    from plangraph.routing_errors import SpawnRefused
    with pytest.raises(SpawnRefused, match='toolless_needs_inline'):
        seat._leg_prompt(result + '\nRead plans/cut.md and verify it.', {}, 'judge', leg,
                         pack=SimpleNamespace(text=''), document_review=False)


def test_toolless_inline_plan_admitted(tmp_path):
    from plangraph.routing_errors import SpawnRefused
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=str(tmp_path))
    leg = SimpleNamespace(harness='claude', tool_mode='prompt-only', max_request_tokens=1000)
    request = 'Read plans/x.md and check its claims.'
    with pytest.raises(SpawnRefused, match='toolless_needs_inline'):
        seat._leg_prompt(request, {}, 'review', leg, document_review=True)
    prompt = request + '\nPlan file plans/x.md:\n```markdown\n# Plan\nCheck the change.\n```'
    assert seat._leg_prompt(prompt, {}, 'review', leg, document_review=True) == prompt


def test_toolless_judge_launches_without_document_review(tmp_path, monkeypatch):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from tools.judge_prompt import build_judge_prompt
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.context_pack import Pack
    from plangraph.spawner.service import SpawnService

    leg = replace(DEFAULT_LEGS[0], tool_mode='inline-only')
    runner = SimpleNamespace(default_cwd=str(tmp_path), provider_responses={})
    service = SpawnService(runner, state=tmp_path / 'state')
    monkeypatch.setattr(service, '_annotate_spawn', lambda *args: Pack())
    launch = AsyncMock(return_value=SimpleNamespace(job_id='fixture', status='completed',
        exit_code=0, served_model=leg.model, served_family=leg.family, served_witness_reason='fixture',
        stdout='VERDICT: FAIL', stderr='', events=[]))
    monkeypatch.setattr(service, '_launch_leg', launch)
    prompt = build_judge_prompt('Check src/x.rs:1', '', {'src/x.rs': 'new', 'cut.rs': None}, {})
    result = asyncio.run(service._spawn(card={'title': 'judge'}, prompt=prompt, role='judge',
                         _plan=SpawnPlan((leg,), (), 'fixture', (), ()), wait_seconds=None))
    assert result['status'] == 'completed'
    launch.assert_awaited_once()
    assert launch.await_args.kwargs['prompt'] == prompt
