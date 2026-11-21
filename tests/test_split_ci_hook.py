"""Exercise preflight delegation through the production hook entry point."""
import io
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_hooks_prepush import HOOK


@pytest.mark.parametrize('ref, fast, expected_fast', [
    ('topic', '1', False),
    ('phase/affected', '1', False),
    ('land/another-branch', '1', False),
    ('batch/example', '1', False),
    ('train/example', '1', True),
    ('land/train-example', '1', True),
    ('land/train-example', '0', False),
])
@pytest.mark.parametrize('result', [0, 1])
def test_hook_preflight_keeps_policy_context(tmp_path, monkeypatch, ref, fast, expected_fast, result):
    pushed_head = '1' * 40
    hook = runpy.run_path(str(HOOK))
    scope = hook['push_main'].__globals__
    # Checkout freshness has its own coverage; isolate preflight delegation.
    monkeypatch.setitem(scope, 'hook_checkout_status', lambda: 0)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(
        f"refs/heads/local {pushed_head} refs/heads/{ref} {'0' * 40}\n"))
    monkeypatch.delenv('PREFLIGHT_SKIP', raising=False)
    monkeypatch.setenv('GIT_INDEX_FILE', 'caller-index')
    monkeypatch.setenv('GIT_DIR', 'caller-git-dir')
    monkeypatch.setenv('PREFLIGHT_FAST', fast)
    monkeypatch.setitem(scope, 'git_out', lambda *a: {
        ('rev-parse', '--show-toplevel'): str(tmp_path),
        ('rev-parse', '--local-env-vars'): 'GIT_DIR\nGIT_INDEX_FILE\n',
    }.get(a, ''))
    size_heads = []
    monkeypatch.setitem(scope, 'file_size_gate', lambda head: size_heads.append(head) or 0)
    monkeypatch.setitem(scope, 'commit_gate', lambda head: 0)
    monkeypatch.setitem(scope, 'base_commit', lambda *a: 'base')
    monkeypatch.setitem(scope, 'affected_base_commit', lambda *a: 'affected-base')
    monkeypatch.setitem(scope, 'main_baselines', lambda *a: [])
    monkeypatch.setitem(scope, 'changelog_docs_warn', lambda *a: None)
    worktrees = []

    def git(*args):
        if args[:2] == ('worktree', 'add'):
            assert args[2] == '--detach'
            assert args[4] == pushed_head
            root = Path(args[3])
            (root / 'tools').mkdir(parents=True)
            (root / 'tools/preflight.py').touch()
            worktrees.append(root)
        elif args[:2] == ('worktree', 'remove'):
            assert args[2:] == ('--force', str(worktrees[0]))
            worktrees.pop()
        return SimpleNamespace(returncode=0, stdout=b'')

    monkeypatch.setitem(scope, 'git', git)
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kw:
                        calls.append((command, kw)) or SimpleNamespace(returncode=result))
    assert hook['push_main']() == result
    assert size_heads == [pushed_head]
    assert len(calls) == 1
    assert worktrees == []
    command, options = calls[0]
    assert Path(command[1]).name == 'preflight.py'
    assert command[command.index('--base') + 1] == 'affected-base'
    assert command[command.index('--scrub-base') + 1] == 'base'
    assert command[command.index('--head') + 1] == pushed_head
    assert ('--fast' in command) == expected_fast
    assert options['cwd'] != tmp_path
    assert Path(command[1]) == options['cwd'] / 'tools/preflight.py'
    assert not options['cwd'].exists()
    # Preserve policy context while isolating the pushed tree from hook-local Git state.
    assert 'GIT_INDEX_FILE' not in options['env']
    assert 'GIT_DIR' not in options['env']
    assert options['env']['PREFLIGHT_FAST'] == fast
    assert options['env']['PREFLIGHT_PYTHON'] == command[0]
