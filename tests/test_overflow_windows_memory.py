"""Local RAM admission must read memory on Windows too (no /proc there).

Measured 2026-11-09: every Windows CLI spawn on main died with
``[Errno 2] No such file or directory: '\\proc\\meminfo'`` before launch.
"""
import os

import pytest

from plangraph.spawner import overflow


def test_windows_branch_does_not_touch_proc(monkeypatch):
    monkeypatch.setattr(overflow.os, 'name', 'nt')
    monkeypatch.setattr(overflow, '_windows_available_memory_mb', lambda: 1234)
    monkeypatch.setattr(overflow, 'Path', lambda *a: pytest.fail('read /proc on Windows'))
    assert overflow.available_memory_mb() == 1234


def test_posix_branch_still_reads_meminfo(monkeypatch, tmp_path):
    meminfo = tmp_path / 'meminfo'
    meminfo.write_text('MemTotal: 9000 kB\nMemAvailable: 4096000 kB\n')
    monkeypatch.setattr(overflow.os, 'name', 'posix')
    monkeypatch.setattr(overflow, 'Path', lambda *a: meminfo)
    assert overflow.available_memory_mb() == 4000


@pytest.mark.skipif(os.name != 'nt', reason='real GlobalMemoryStatusEx needs Windows')
def test_real_windows_memory_is_positive():
    assert overflow._windows_available_memory_mb() > 0
