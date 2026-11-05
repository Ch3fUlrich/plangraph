"""Hermetic guards for actionable prompt text and public repository pointers."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_tail_standing_question():
    text = (ROOT / 'src/plangraph/review/tail.md').read_text()
    assert 'Does the change game tests' in text
