"""WSL hop invocation refusals and the PATH-less default."""
import json

import pytest

from plangraph.spawner import remote_ps


def test_wsl_hop_without_path_unchanged():
    command, body = remote_ps.invocation({'wsl_distro': 'Ubuntu', 'env': {}}, 'true')
    assert command == 'wsl.exe --distribution Ubuntu --exec pg-spawnd steps'
    assert json.loads(body) == {'steps': [{'argv': ['true']}]}


@pytest.mark.parametrize('path', ['/bin;&echo', '/bin:%TEMP%', '/bin:$HOME/bin', '/bin\n/bin',
                                  '/bin::/usr/bin', 'bin:/usr/bin', '', None])
def test_wsl_hop_rejects_invalid_path(path):
    with pytest.raises(ValueError, match='invalid WSL PATH'):
        remote_ps.invocation({'wsl_distro': 'Ubuntu', 'env': {'PATH': path}}, 'true')
