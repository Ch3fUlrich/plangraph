"""Hermetic guards for actionable prompt text and public repository pointers."""
from pathlib import Path
import subprocess
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
    tails = subprocess.check_output(['git', 'ls-files', '*tail.md'], cwd=ROOT, text=True).splitlines()
    assert tails == ['src/plangraph/review/tail.md']  # load_tail reads this sole source.
    from plangraph.review.prompt import load_tail
    assert load_tail() == text
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
    assert not re.search(r'(?:[\w.-]+/)+[\w.-]*DECISIONS[\w.-]*', text, re.I)


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


def test_toolchain_provision_not_version_claim():
    text = (ROOT / 'skills/plangraph-planning/SKILL.md').read_text()
    assert 'The spawner provides the toolchain in the brief' in text
    assert '_leg_prompt' not in text


def test_l3_question_channel():
    text = (ROOT / 'skills/unattended-orchestration/SKILL.md').read_text()
    row = next(line for line in text.splitlines() if line.startswith('| **L3**'))
    assert 'never' not in row
    assert 'PG_QUESTION:' in row
    assert 'rust/worker/src/question.rs:QUESTION_PREFIX' in row
    code = (ROOT / 'rust/worker/src/question.rs').read_text()
    assert 'pub const QUESTION_PREFIX: &str = "PG_QUESTION:";' in code


def test_report_lead_in_and_retry_pointers():
    text = (ROOT / 'skills/implement/SKILL.md').read_text()
    assert 'End with these, and no claim without evidence:\n\n- `commits:`' in text
    text = (ROOT / 'docs/operations/spawner-prompt-rules.md').read_text()
    for pointer in ('ReviewMethod.max_rounds', 'ATTEMPT_BUDGET', 'plan.legs', 'no_fallthrough'):
        assert pointer in text
    assert 'no numeric\nfallthrough constant' in text
    assert all(len(line) <= 100 for line in text.splitlines() if
               'render_fixer_brief` numbers' in line or 'checklist or use' in line)


def test_spawner_size_and_prompt_rules_pointer():
    text = (ROOT / 'docs/SPAWNER.md').read_text()
    assert len(text.splitlines()) <= 728
    assert text.count('(operations/spawner-prompt-rules.md)') == 1
    rules = (ROOT / 'docs/operations/spawner-prompt-rules.md').read_text()
    assert len(rules.splitlines()) <= 400
    for contract in ('render_fixer_brief', 'refuted by a named test', '`not done:`',
                     'reason', 'Never echo an unchecked checklist', '`Remaining gaps`',
                     'python3 tools/affected_tests.py --base origin/main --run',
                     'COMMIT before finishing'):
        assert contract in rules


def test_spawner_merge_retains_package_and_runtime_contracts_once():
    text = (ROOT / 'docs/SPAWNER.md').read_text()
    for contract in (
        'D-5840: implementer and reviewer reasoning defaults to medium.',
        'Package sessions use `plangraph_package_launch',
        'Freshness admission and refused-worktree cleanup use the shared checkout Git environment',
        'Quota polling considers only the newest request of a session',
        'The live-descendant sampling flag is observed only on Linux.',
        'OOM wrapper lines are recognized by their known event shape',
    ):
        assert text.count(contract) == 1, contract
