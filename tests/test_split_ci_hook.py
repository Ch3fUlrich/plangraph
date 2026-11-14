"""Exercise preflight delegation through the production hook entry point."""
import io
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_hooks_prepush import HOOK


@pytest.mark.parametrize('result', [0, 1])
def test_hook_preflight_keeps_policy_context(tmp_path, monkeypatch, result):
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'preflight.py').touch()
    hook = runpy.run_path(str(HOOK))
    scope = hook['push_main'].__globals__
    monkeypatch.setattr(sys, 'stdin', io.StringIO(
        f"refs/heads/topic {'1' * 40} refs/heads/topic {'0' * 40}\n"))
    monkeypatch.delenv('PREFLIGHT_SKIP', raising=False)
    monkeypatch.setenv('GIT_INDEX_FILE', 'caller-index')
    monkeypatch.setitem(scope, 'git_out', lambda *a: str(tmp_path) if a[0] == 'rev-parse' else '')
    monkeypatch.setitem(scope, 'file_size_gate', lambda: 0)
    monkeypatch.setitem(scope, 'base_commit', lambda *a: 'base')
    monkeypatch.setitem(scope, 'affected_base_commit', lambda *a: 'affected-base')
    monkeypatch.setitem(scope, 'main_baselines', lambda *a: [])
    monkeypatch.setitem(scope, 'changelog_docs_warn', lambda *a: None)
    monkeypatch.setitem(scope, 'git', lambda *a: SimpleNamespace(returncode=0))
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: 'GIT_DIR\nGIT_INDEX_FILE\n')
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kw:
                        calls.append((command, kw)) or SimpleNamespace(returncode=result))
    assert hook['push_main']() == result
    command, options = calls[0]
    assert Path(command[1]).name == 'preflight.py'
    assert command[command.index('--base') + 1] == 'affected-base'
    assert command[command.index('--scrub-base') + 1] == 'base'
    # Omitted env inherits the caller; an explicit env must preserve policy context.
    import os
    assert options.get('env', os.environ)['GIT_INDEX_FILE'] == 'caller-index'
