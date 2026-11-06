"""Hermetic process evidence for watchdog CLI tests."""
import pytest


@pytest.fixture(autouse=True)
def isolated_process_evidence(monkeypatch, tmp_path_factory):
    from plangraph.spawner import stuck_watch
    proc_root = tmp_path_factory.mktemp('isolated-proc')
    real_processes = stuck_watch.processes
    monkeypatch.setattr(stuck_watch, 'processes', lambda proc: real_processes(proc_root))
