"""Supervisor stderr rotation keeps the log path present at every instant."""
import logging
import os
from pathlib import Path

from plangraph.spawner import cli


def _watch_replace(monkeypatch, log, seen):
    real_replace = os.replace

    def watching_replace(src, dst):
        seen.append(('before', log.exists()))
        result = real_replace(src, dst)
        seen.append(('after', log.exists()))
        return result

    monkeypatch.setattr(os, 'replace', watching_replace)


def test_rotation_never_leaves_the_log_path_absent(monkeypatch, tmp_path):
    log = tmp_path / 'supervisor-x.stderr.log'
    log.write_bytes(b'old line\n')
    log.chmod(0o600)
    seen = []
    _watch_replace(monkeypatch, log, seen)
    cli._rotate_log(str(log))
    assert {when for when, _ in seen} == {'before', 'after'}
    assert all(present for _, present in seen)
    assert Path(str(log) + '.1').read_bytes() == b'old line\n'
    assert log.read_bytes() == b''
    if os.name != 'nt':
        assert log.stat().st_mode & 0o777 == 0o600
        assert (Path(str(log) + '.1').stat().st_mode & 0o777) == 0o600


def test_rotation_of_a_missing_log_is_a_noop_that_creates_it(tmp_path):
    log = tmp_path / 'supervisor-y.stderr.log'
    cli._rotate_log(str(log))
    assert log.read_bytes() == b''


def test_mcp_launch_handler_rotates_without_a_gap(monkeypatch, tmp_path):
    """The fresh_spawn (MCP) supervisor uses this handler; its rollover must not rename the log away."""
    log = tmp_path / 'supervisor-z.stderr.log'
    handler = cli._private_rotating_log(str(log), max_bytes=64)
    seen = []
    _watch_replace(monkeypatch, log, seen)
    for index in range(6):
        handler.emit(logging.makeLogRecord(dict(msg=f'line {index} ' + 'x' * 30 + '\n', args=())))
    handler.close()
    assert seen and all(present for _, present in seen)
    assert Path(str(log) + '.1').stat().st_size <= 64
    assert log.exists() and log.stat().st_size <= 64


def test_rotation_source_is_self_contained_for_an_older_run_tree(tmp_path):
    """The bootstrap runs against the run tree's own cli, which may lack the helpers: they travel as source."""
    namespace = {'os': os}
    exec(cli._rotation_source(), namespace)
    log = tmp_path / 'supervisor-w.stderr.log'
    log.write_bytes(b'x\n')
    namespace['_rotate_log'](str(log))
    assert Path(str(log) + '.1').read_bytes() == b'x\n' and log.read_bytes() == b''
    assert callable(namespace['_private_rotating_log'])


def test_rotation_only_source_leaves_the_handler_out():
    assert '_private_rotating_log' not in cli._rotation_source(handler=False)
    assert 'def _rotate_log' in cli._rotation_source(handler=False)
