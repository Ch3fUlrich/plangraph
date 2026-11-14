from test_hooks_prepush import *

def test_affected_gate_is_file_only_and_time_bounded(tmp_path, monkeypatch):
    import runpy
    from types import SimpleNamespace
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'affected_tests.py').touch()
    monkeypatch.setenv('PREFLIGHT_SKIP', '1')
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kw:
                        calls.append((command, kw)) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: 'GIT_DIR\n')
    hook = runpy.run_path(str(HOOK))
    assert hook['affected_gate'](tmp_path, 'base', 'head') == 0
    command, options = calls[0]
    assert '--files-only' in command and '--run' in command
    assert Path(command[1]).name == 'affected_tests.py'
    assert options['timeout'] <= 240
    assert 'preflight.py' not in ' '.join(command)
