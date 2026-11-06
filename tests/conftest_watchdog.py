"""Hermetic process evidence for watchdog CLI tests."""
import pytest


@pytest.fixture(autouse=True)
def isolated_process_evidence(monkeypatch):
    from plangraph.spawner import stuck_watch
    monkeypatch.setattr(stuck_watch, 'processes', lambda proc: {})
