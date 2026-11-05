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


def test_no_unresolvable_keeper_instruction():
    historical = {}  # No historical mentions currently need an exception/reason.
    for folder in ('skills', 'docs', 'src'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file():
                text = path.read_bytes().decode('utf-8', errors='replace')
                if 'TEST-SPEED-AFFECTED' in text:
                    assert historical.get(path.relative_to(ROOT).as_posix())


def test_tail_standing_question():
    from plangraph.review.prompt import assemble
    text = (ROOT / 'src/plangraph/review/tail.md').read_text()
    assert 'Does the change game tests' in text
    prompt = assemble(['check'], [])
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
