"""Hermetic guards for actionable prompt text and public repository pointers."""
from pathlib import Path
import re
import subprocess

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
    text = (ROOT / 'src/plangraph/review/tail.md').read_text()
    assert 'Does the change game tests' in text


def test_claude_skill_pointers():
    text = (ROOT / 'CLAUDE.md').read_text()
    pointers = re.findall(r'`(skills/[^`]+/SKILL\.md)`', text)
    assert pointers
    assert all((ROOT / pointer).is_file() for pointer in pointers)
    assert 'AGENTS.md' not in text


def test_no_missing_protocol_pointer():
    paths = subprocess.check_output(
        ['git', 'ls-files', '-z', 'skills/', 'docs/'], cwd=ROOT).split(b'\0')
    for name in filter(None, paths):
        text = (ROOT / name.decode()).read_bytes().decode('utf-8', errors='replace')
        assert 'docs/agent-protocol.md' not in text


def test_l3_question_channel():
    text = (ROOT / 'skills/unattended-orchestration/SKILL.md').read_text()
    row = next(line for line in text.splitlines() if line.startswith('| **L3**'))
    assert 'never' not in row
    assert 'NEEDS_INPUT' in row
