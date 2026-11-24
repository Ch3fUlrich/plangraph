"""The trailers a feeder batch brief hands the writer must satisfy the commit-message gate."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import ci_commit_gate  # noqa: E402

FIXTURE = ROOT / 'rust/pg-spawnd/tests/fixtures/feeder-batch-input.txt'


def _trailers():
    prefix, payload = FIXTURE.read_text().split('\n', 1)
    assert prefix == 'PLANGRAPH-BATCH-INPUT-V1'
    return json.loads(payload)['run']['trailers']


def test_feeder_batch_trailers_carry_a_writer_and_pass_the_commit_gate():
    trailers = _trailers()
    assert any(line.startswith('Writer: ') for line in trailers)
    assert sum(line.startswith('Plan-Item: ') for line in trailers) == 2
    # The brief's Changelog line is guidance; a commit names a fragment or skips.
    block = [line for line in trailers if not line.startswith('Changelog:')]
    message = 'Fix a thing\n\nBody.\n\n' + '\n'.join(block + ['Changelog: skip (fixture)']) + '\n'
    assert ci_commit_gate.message_violations(message) == []


def test_a_batch_without_a_writer_trailer_fails_the_gate():
    block = [line for line in _trailers() if not line.startswith(('Writer: ', 'Changelog:'))]
    message = 'Fix a thing\n\nBody.\n\n' + '\n'.join(block) + '\n'
    assert ci_commit_gate.MISSING_WRITER in ci_commit_gate.message_violations(message)
