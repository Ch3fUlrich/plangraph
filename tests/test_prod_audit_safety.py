"""Installed HEAD audit hooks must never obstruct fixture Git operations."""
import json
import os
import subprocess
import time

import pytest

from plangraph.fleet_sync import drift


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    run = lambda *args: subprocess.check_output(['/usr/bin/git', '-C', str(root), *args], text=True, stderr=subprocess.PIPE).strip()
    run('init', '-b', 'main')
    run('config', 'user.name', 'Fixture')
    run('config', 'user.email', 'fixture@example.invalid')
    run('commit', '--allow-empty', '-m', 'one')
    first = run('rev-parse', 'HEAD')
    run('commit', '--allow-empty', '-m', 'two')
    state = tmp_path / 'state'
    drift.install_audit(root, state, apply=True)
    return root, state, first, run


@pytest.mark.parametrize('fault', ['missing', 'broken', 'no-python-path'])
def test_audit_failures_do_not_block_git(checkout, monkeypatch, fault):
    _root, state, first, run = checkout
    script = state / 'head-audit.py'
    if fault == 'missing':
        script.unlink()
    elif fault == 'broken':
        script.write_text('this is not valid Python!\n')
    monkeypatch.delenv('PYTHONPATH', raising=False)
    if fault == 'no-python-path':
        monkeypatch.setenv('PATH', '/unavailable')
    run('checkout', '--detach', first)
    assert run('rev-parse', 'HEAD') == first
    run('update-ref', 'HEAD', run('rev-parse', 'main'))
    assert run('rev-parse', 'HEAD') == run('rev-parse', 'main')


def test_prepared_and_aborted_never_start_runtime(checkout):
    root, state, _, _ = checkout
    marker = state / 'runtime-started'
    (state / 'head-audit.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    hook = root / '.git/hooks/reference-transaction'
    for phase in ['prepared', 'aborted']:
        result = subprocess.run([str(hook), phase], cwd=root, input='', text=True, capture_output=True, check=False)
        assert result.returncode == 0
    assert not marker.exists()


def test_one_runtime_per_checkout_and_linked_tree_identity(checkout, tmp_path):
    __root, state, first, run = checkout
    linked = tmp_path / 'linked'
    run('worktree', 'add', '-b', 'linked-branch', str(linked))
    script = state / 'head-audit.py'
    # Count starts independently of log dedupe; both hooks share the runtime.
    original = script.read_text()
    counter = state / 'starts'
    script.write_text(f'with open({str(counter)!r}, "a") as f: f.write(repr(__import__("sys").argv)+"\\n")\n' + original)
    counter.write_text('')
    subprocess.run(['/usr/bin/git', '-C', str(linked), 'checkout', '--detach', first], check=True, capture_output=True)
    assert len(counter.read_text().splitlines()) == 1, counter.read_text()
    rows = [json.loads(line) for line in (state / 'head-moves.jsonl').read_text().splitlines()]
    assert rows[-1]['worktree'] == str(linked)
    assert rows[-1]['branch'] == 'detached'
    assert run('symbolic-ref', '--short', 'HEAD') == 'main'
    subprocess.run(['/usr/bin/git', '-C', str(linked), 'checkout', 'linked-branch'], check=True, capture_output=True)
    rows = [json.loads(line) for line in (state / 'head-moves.jsonl').read_text().splitlines()]
    assert rows[-1]['worktree'] == str(linked) and rows[-1]['branch'] == 'linked-branch'


def test_stalled_audit_is_bounded(checkout):
    _root, state, first, run = checkout
    # Opening a FIFO as the log is a real blocking I/O failure, not a fake timer.
    os.mkfifo(state / 'head-moves.jsonl')
    start = time.monotonic()
    run('checkout', '--detach', first)
    assert time.monotonic() - start < 1.8
    assert run('rev-parse', 'HEAD') == first


def test_log_rotation_keeps_one_previous_file(checkout):
    _root, state, first, run = checkout
    log = state / 'head-moves.jsonl'
    log.write_bytes(b'x' * (1024 * 1024))
    run('checkout', '--detach', first)
    backup = log.with_suffix('.jsonl.1')
    assert backup.stat().st_size == 1024 * 1024
    assert log.stat().st_size < 4096
    assert json.loads(log.read_text())['new_sha'] == first
    run('update-ref', 'HEAD', run('rev-parse', 'main'))
    assert len(list(state.glob('head-moves.jsonl.*'))) == 1


def _concurrent_append(state, branch, ready, outcomes):
    from plangraph.fleet_sync.head_audit import record

    ready.wait(timeout=10)
    try:
        record(state, '/fixture/' + branch, 'a' * 40, 'b' * 40, branch, state, max_bytes=1)
    except OSError as exc:
        outcomes.put((branch, type(exc).__name__))
    else:
        outcomes.put((branch, None))


def test_concurrent_appends_survive_lock_contention_and_rotation(tmp_path):
    import fcntl
    import multiprocessing
    import queue

    context = multiprocessing.get_context('spawn')
    ready = context.Barrier(3)
    outcomes = context.Queue()
    processes = [context.Process(target=_concurrent_append, args=(str(tmp_path), name, ready, outcomes))
                 for name in ['first', 'second']]
    with (tmp_path / 'head-audit.lock').open('wb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            for process in processes:
                process.start()
            ready.wait(timeout=10)
            # Both must wait for the held rotation lock rather than drop a line.
            with pytest.raises(queue.Empty):
                outcomes.get(timeout=0.2)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)
    assert all(process.exitcode == 0 for process in processes)
    assert sorted(outcomes.get(timeout=2) for _ in processes) == [('first', None), ('second', None)]
    rows = [json.loads(line) for path in tmp_path.glob('head-moves.jsonl*')
            for line in path.read_text().splitlines()]
    assert {row['branch'] for row in rows} == {'first', 'second'}
    assert len(rows) == 2
