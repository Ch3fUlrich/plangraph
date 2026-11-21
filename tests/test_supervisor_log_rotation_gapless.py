"""Supervisor stderr rotation keeps the log path present at every instant."""
import os
from pathlib import Path

from plangraph.spawner import cli


def test_rotation_never_leaves_the_log_path_absent(monkeypatch, tmp_path):
    log = tmp_path / 'supervisor-x.stderr.log'
    log.write_bytes(b'old line\n')
    log.chmod(0o600)
    seen = []
    real_replace = os.replace

    def watching_replace(src, dst):
        seen.append(log.exists())
        return real_replace(src, dst)

    monkeypatch.setattr(os, 'replace', watching_replace)
    cli._rotate_log(str(log))
    assert seen and all(seen)
    assert Path(str(log) + '.1').read_bytes() == b'old line\n'
    assert log.read_bytes() == b''
    if os.name != 'nt':
        assert log.stat().st_mode & 0o777 == 0o600
        assert (Path(str(log) + '.1').stat().st_mode & 0o777) == 0o600


def test_rotation_of_a_missing_log_is_a_noop_that_creates_it(tmp_path):
    log = tmp_path / 'supervisor-y.stderr.log'
    cli._rotate_log(str(log))
    assert log.read_bytes() == b''
