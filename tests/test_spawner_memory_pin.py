"""Spawner tests never let the host's real MemAvailable decide an admission."""
from pathlib import Path

import pytest

from plangraph.spawner import overflow, seat_spawn


def test_unpinned_real_reader_fails_the_test():
    with pytest.raises(AssertionError, match='real host memory reader'):
        overflow.available_memory_mb()


def test_partly_patched_read_text_does_not_hide_the_real_reader(monkeypatch):
    """A test that patches Path.read_text for other files still fails when /proc/meminfo is read for real."""
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda self, *a, **kw:
                        'unrelated' if str(self) == '/elsewhere' else original(self, *a, **kw))
    with pytest.raises(AssertionError, match='real host memory reader'):
        overflow.available_memory_mb()


def test_fixture_value_decides_not_the_host(admission_memory, monkeypatch):
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda self, *a, **kw:
                        'MemAvailable: 1024 kB\n' if str(self) == '/proc/meminfo' else original(self, *a, **kw))
    admission_memory(64000)
    assert overflow.available_memory_mb() == 64000
    assert seat_spawn.resources(None) == (0, 64000)


def test_default_reading_is_the_pinned_value():
    """No fake meminfo and no pin: the autouse default answers, the host is not read."""
    assert seat_spawn.resources(None) == (0, 64000)


def spawn_admission(tmp_path, monkeypatch):
    """seat_spawn.admit for a local implement spawn, with only /proc/meminfo left to the test."""
    from types import SimpleNamespace
    from conftest import use_projected_overflow_config
    from plangraph.spawner import host_ready
    monkeypatch.setattr(host_ready, 'disk_free_gb', lambda path: 100)
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    use_projected_overflow_config(monkeypatch, tmp_path / 'absent')
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(tmp_path / 'checkout')),
                              ps=lambda: {'runs': []})
    return lambda: seat_spawn.admit(service, {})


def test_unpinned_spawn_admission_fails_when_it_reaches_the_real_reader(tmp_path, monkeypatch):
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda self, *a, **kw:
                        'unrelated' if str(self) == '/elsewhere' else original(self, *a, **kw))
    admit = spawn_admission(tmp_path, monkeypatch)
    with pytest.raises(AssertionError, match='real host memory reader'):
        admit()


def test_pinned_spawn_admission_sees_the_fixture_value(tmp_path, monkeypatch, admission_memory):
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda self, *a, **kw:
                        'MemAvailable: 100 kB\n' if str(self) == '/proc/meminfo' else original(self, *a, **kw))
    admission_memory(100)
    refusal = spawn_admission(tmp_path, monkeypatch)()
    assert refusal is not None and 'free 100 MB' in refusal['reason']
    admission_memory(64000)
    assert spawn_admission(tmp_path, monkeypatch)() is None
