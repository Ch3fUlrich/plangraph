import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from plangraph.spawner import remote_ps


def configure(tmp_path, monkeypatch, **fields):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    path = tmp_path / 'plangraph' / 'ps-hosts.json'
    path.parent.mkdir()
    host = dict(name='node-a', target='target', **fields)
    path.write_text(json.dumps({'hosts': [host]}))
    return host


@pytest.mark.parametrize('win_src', ['C:/opt/plangraph/src',
                                   r'C:\opt\plangraph\src',
                                   'd:/opt/plan_graph-1.2/src', 'C:/', 'C:/source space'])
def test_windows_native_command(tmp_path, monkeypatch, win_src):
    host = configure(tmp_path, monkeypatch, win_src=win_src)
    assert remote_ps.configured_hosts() == [host]
    original = subprocess.Popen

    def run(argv, **kwargs):
        assert argv == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
                        '--', 'target',
                        f'if not exist "{win_src}\\" exit /b 44 & '
                        f'set "PYTHONPATH={win_src}"&& python -m plangraph.spawner.remote_ps']
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['stdout'] == subprocess.PIPE
        assert kwargs['stderr'] == subprocess.DEVNULL
        assert kwargs.get('shell', False) is False
        return original([sys.executable, '-c', 'print(\'{"runs": []}\', end="")'],
                        **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', run)
    assert remote_ps.query_host(host, 2) == '{"runs": []}'


@pytest.mark.parametrize('win_src', [
    None, True, 1, [], {}, '', 'relative/src', '/opt/plangraph/src',
    'C:src', 'C', 'CC:/src', '1:/src', r'\\server\src',
    'C:/src\n', 'C:/src\r', 'C:/src\t', 'C:/é',
    *['C:/opt/a' + char + 'b/src' for char in '&\"\';|$%^<>`()=:!+*?'],
])
def test_invalid_windows_source_never_reaches_ssh(tmp_path, monkeypatch, win_src):
    configure(tmp_path, monkeypatch, win_src=win_src)
    assert remote_ps.configured_hosts() == [dict(name='invalid-config', invalid=True)]
    transport = Mock(side_effect=AssertionError('invalid config reached SSH'))
    local = [dict(run_id='local', status='running')]
    assert remote_ps.list_runs(local, transport=transport) == local + [
        dict(host='invalid-config', status='unknown', reason='host query failed')]
    transport.assert_not_called()


@pytest.mark.parametrize('other', [
    dict(src='/opt/plangraph/src'), dict(wsl_distro='Ubuntu'),
    dict(src='/opt/plangraph/src', wsl_distro='Ubuntu'),
    dict(src=None), dict(wsl_distro=None),
])
def test_windows_source_excludes_posix_and_wsl(tmp_path, monkeypatch, other):
    configure(tmp_path, monkeypatch, win_src='C:/opt/plangraph/src', **other)
    assert remote_ps.configured_hosts() == [dict(name='invalid-config', invalid=True)]
