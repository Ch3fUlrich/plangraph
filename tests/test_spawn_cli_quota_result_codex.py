"""A native Codex writer's usage-limit stop is booked in the leg state the route reads."""
import time

from plangraph.spawner.cli import main
from plangraph.spawner.leg_state import LegStateStore


def test_quota_result_books_the_codex_writer_leg_until_the_reset(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    reset = time.time() + 3600
    assert main(['--state', str(tmp_path), 'quota-result', '--model', 'gpt-6.2-sol',
                 '--harness', 'codex', '--error', 'Error: usage limit reached.',
                 '--reset-at', str(reset)]) == 0
    store = LegStateStore(tmp_path / 'plangraph' / 'leg-state.sqlite')
    status = store.leg_status('gpt-6.2-sol', harness='codex')
    assert status['state'] == 'limited', status
    assert store.leg_status('gpt-6.2-sol-other', harness='codex')['state'] == 'ok'
