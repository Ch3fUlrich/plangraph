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
