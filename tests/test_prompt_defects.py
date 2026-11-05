"""Hermetic guards for actionable prompt text and public repository pointers."""
from pathlib import Path
import re
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_fixer_completion_contract():
    from plangraph.routing_support.fixer import render_fixer_brief
    text = render_fixer_brief([{'text': 'repair'}])
    assert 'python3 tools/affected_tests.py --base origin/main --run' in text
    for condition in ('fixed', 'refuted by a named test', 'not done:', 'reason'):
        assert condition in text
    assert 'TEST-SPEED-AFFECTED' not in text


def test_fixer_terminal_report_gate(tmp_path):
    import asyncio
    from plangraph.routing_support.fixer import render_fixer_brief
    from plangraph.spawner.service import SpawnService
    brief = render_fixer_brief([{'text': 'repair'}])
    assert 'reported as not done: prose with the reason' in brief
    assert 'Never echo an unchecked checklist' in brief
    assert 'Do not use a Remaining gaps section for terminal not done: items' in brief
    service = SpawnService(runner=object(), state=tmp_path)
    async def probe():
        for output, reason in [('not done: repair; needs a decision', 'clean_report'),
                               ('1. [ ] repair', 'remaining_gaps'),
                               ('## Remaining gaps\nrepair', 'remaining_gaps')]:
            record = dict(run_id='probe', role='implement', exit_code=0)
            if reason == 'remaining_gaps':
                record['continuation_run_id'] = 'existing'  # Inspect gate without spawning.
            await service._continue_writer(record, output)
            assert record['continuation_reason'] == reason
    asyncio.run(probe())


def test_no_unresolvable_keeper_instruction():
    # Main documents this exact token as jargon to avoid, not an instruction.
    explanatory = {
        'skills/plangraph-planning/references/task-contract.md':
            '| Fleet jargon an agent cannot resolve (`TEST-SPEED-AFFECTED`, bare D-ids) | It cannot look them up |',
    }
    for folder in ('skills', 'docs', 'src'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file():
                text = path.read_bytes().decode('utf-8', errors='replace')
                for line in text.splitlines():
                    if 'TEST-SPEED-AFFECTED' in line:
                        assert line == explanatory.get(path.relative_to(ROOT).as_posix())


@pytest.mark.parametrize('name', ['docs/brief.md',
    'skills/plangraph-planning/references/task-contract.md'])
def test_keeper_scan_rejects_instructions(tmp_path, monkeypatch, name):
    monkeypatch.setitem(globals(), 'ROOT', tmp_path)
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text('Run affected tests only; follow TEST-SPEED-AFFECTED.\n')
    with pytest.raises(AssertionError):
        test_no_unresolvable_keeper_instruction()


@pytest.mark.parametrize('mode', ['files', 'diff'])
def test_tail_standing_question(mode):
    from plangraph.review.prompt import assemble_all, assemble_diff
    text = (ROOT / 'src/plangraph/review/tail.md').read_text()
    assert 'Does the change game tests' in text
    prompts = (assemble_all(['check'], {'src/example.py': 'value = 1\n'})
               if mode == 'files' else assemble_diff(['check'],
                   'diff --git a/src/example.py b/src/example.py\n'
                   '--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n-value = 0\n+value = 1\n'))
    assert prompts
    for prompt in prompts:
        assert prompt.endswith(text.rstrip())
        assert prompt.count('Does the change game tests') == 1


def test_claude_skill_pointers():
    text = (ROOT / 'CLAUDE.md').read_text()
    pointers = re.findall(r'`(skills/[^`]+/SKILL\.md)`', text)
    assert pointers
    assert all((ROOT / pointer).is_file() for pointer in pointers)
    assert 'AGENTS.md' not in text
    assert 'fleet decisions file' not in text


def test_no_missing_protocol_pointer():
    for folder in ('skills', 'docs'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file():
                text = path.read_bytes().decode('utf-8', errors='replace')
                assert 'docs/agent-protocol.md' not in text


def test_protocol_scan_includes_untracked_files(tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), 'ROOT', tmp_path)
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs/untracked.md').write_text('docs/agent-protocol.md')
    with pytest.raises(AssertionError):
        test_no_missing_protocol_pointer()


def test_skill_card_pointers():
    skill = ROOT / 'skills/unattended-orchestration/SKILL.md'
    pointers = re.findall(r'cards/[\w-]+\.md', skill.read_text())
    assert pointers
    assert all((skill.parent / pointer).is_file() for pointer in pointers)


def test_toolchain_is_callers_responsibility():
    text = (ROOT / 'skills/plangraph-planning/SKILL.md').read_text()
    assert 'The caller must put lint, test and format commands' in text
    assert 'The spawner provides the toolchain' not in text


def test_l3_question_channel():
    text = (ROOT / 'skills/unattended-orchestration/SKILL.md').read_text()
    row = next(line for line in text.splitlines() if line.startswith('| **L3**'))
    assert 'never' not in row
    assert 'NEEDS_INPUT' in row
