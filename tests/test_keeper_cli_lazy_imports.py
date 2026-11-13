"""Door-path imports are checked in fresh interpreters, not pytest's module cache."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('command', ['help', 'whoami', 'admin', 'oversight'])
def test_keeper_cli_lazy_imports(tmp_path, command):
    config = tmp_path / 'keeper.json'
    config.write_text(json.dumps(dict(db=str(tmp_path / 'keeper.db'),
                                     snapshot=str(tmp_path / 'snapshot.json'))))
    config.chmod(0o600)
    code = '''import importlib.abc, sys
from pathlib import Path
command, config, out = sys.argv[1:]
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if command in ('help', 'whoami') and fullname == 'fcntl':
            raise ModuleNotFoundError('fcntl unavailable')
sys.meta_path.insert(0, Guard())
from plangraph.keeper import cli
deferred = ('cli_admin', 'db', 'oversight')
assert all('plangraph.keeper.' + name not in sys.modules for name in deferred)
if command == 'help':
    try: cli.main(['--help'])
    except SystemExit as exc: assert exc.code == 0
elif command == 'whoami':
    cli.Session.call = lambda *a, **k: dict(name='reader', role='reader')
    assert cli.main(['whoami', '--json']) == 0
elif command == 'admin':
    assert cli.main(['--config', config, 'admin', 'init']) == 0
    assert cli.main(['--config', config, 'admin', 'verify']) == 0
    assert 'plangraph.keeper.cli_admin' in sys.modules
    assert 'plangraph.keeper.db' in sys.modules
    assert 'plangraph.keeper.oversight' not in sys.modules
else:
    assert cli.main(['--config', config, 'oversight-check', '--out-dir', out,
                     '--no-alarms']) == 0
    assert 'plangraph.keeper.db' in sys.modules
    assert 'plangraph.keeper.oversight' in sys.modules
    assert 'plangraph.keeper.cli_admin' not in sys.modules
    assert (Path(out) / 'digest.pair.json').is_file()
if command in ('help', 'whoami'):
    assert all('plangraph.keeper.' + name not in sys.modules for name in deferred)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'),
               PG_KEEPER_BACKEND='sqlite')
    argv = [sys.executable, '-c', code]
    if command == 'oversight':
        subprocess.run([*argv, 'admin', str(config), str(tmp_path)], env=env,
                       check=True, capture_output=True, timeout=30)
    result = subprocess.run([*argv, command, str(config), str(tmp_path)], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
