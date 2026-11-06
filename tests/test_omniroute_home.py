import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / 'deploy/omniroute'
SPEC = importlib.util.spec_from_file_location('export_combos', HOME / 'export-combos.py')
EXPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORT)
COPIED = '''apply-capability-overrides.ps1 apply-custom-models.sh apply.ps1 apply.sh
capability-overrides.json combos.json context-overrides.json custom-models.json
live/server-combos.json patch-gateway-clamp.ps1 patches/0001-clamp-max-tokens.patch
qwen-clamp-reapply-README.md qwen-clamp-reapply.ps1 reason-fix-README.md
reason-fix-reapply.ps1 reasoning-defense.md verify-clamp.mjs vertex-failover-knobs.json
vertex-trailing-turn-README.md'''.split()
CHANGED = set('''apply-capability-overrides.ps1 apply-custom-models.sh apply.ps1 apply.sh
qwen-clamp-reapply-README.md qwen-clamp-reapply.ps1 reason-fix-README.md reason-fix-reapply.ps1
reasoning-defense.md verify-clamp.mjs live/server-combos.json vertex-failover-knobs.json'''.split())


@pytest.mark.parametrize('name', ['apply.sh', 'apply-custom-models.sh'])
@pytest.mark.parametrize('setting', [None, 'AUTOOS_OMNIROUTE_URL', 'OMNIROUTE_PORT'])
def test_gateway_selection(name, setting, tmp_path):
    env = dict(os.environ, PLANGRAPH_AUTOOS_ROOT=str(tmp_path))
    for key in ('AUTOOS_OMNIROUTE_URL', 'OMNIROUTE_PORT'):
        env.pop(key, None)
    if setting:
        env[setting] = 'http://127.0.0.1:9' if setting.endswith('URL') else '9'
    text = (HOME / name).read_text()
    # Execute the actual initialization only: no CLI or network access.
    prefix = text[:text.index('GATEWAY=')]
    assignment = text[text.index('GATEWAY='):].splitlines()[0]
    script = tmp_path / name
    script.write_text(prefix + assignment + '\nprintf "%s" "$GATEWAY"')
    result = subprocess.run(['bash', str(script)],
                            env=env, capture_output=True, text=True)
    if setting:
        assert result.returncode == 0
        assert result.stdout == 'http://127.0.0.1:9'
    else:
        assert result.returncode == 2
        assert all(key in result.stderr for key in ('AUTOOS_OMNIROUTE_URL', 'OMNIROUTE_PORT'))


@pytest.mark.parametrize('name', ['apply.ps1', 'apply-capability-overrides.ps1'])
def test_powershell_gateway_contract(name):
    text = (HOME / name).read_text()
    assert "if (-not $env:AUTOOS_OMNIROUTE_URL -and -not $env:OMNIROUTE_PORT) { throw 'Set AUTOOS_OMNIROUTE_URL or OMNIROUTE_PORT before applying.' }" in text
    assert '$Gateway = if ($env:AUTOOS_OMNIROUTE_URL) { $env:AUTOOS_OMNIROUTE_URL } else { "http://127.0.0.1:$($env:OMNIROUTE_PORT)" }' in text


def test_files_paths_and_shapes():
    for name in COPIED + ['README.md', 'export-combos.py', 'combo-set/combos.json']:
        assert (HOME / name).is_file()
    for path in HOME.rglob('*'):
        if path.is_file() and path.name != 'README.md' and '__pycache__' not in path.parts:
            assert 'configuration/omniroute' not in path.read_text().replace('\\', '/')
    source = json.loads((HOME / 'live/server-combos.json').read_text())['combos']
    rows = json.loads((HOME / 'combo-set/combos.json').read_text())['combos']
    assert len(rows) == len(source) == 34
    assert rows == [{'name': n, 'members': m} for n, m in sorted(source.items())]
    for row in rows:
        assert row['name'] and row['members']
        assert 'strategy' not in row
    plan = json.loads((HOME / 'combos.json').read_text())
    assert isinstance(plan, dict) and plan != {'combos': rows}
    for name in ['apply.sh', 'apply-custom-models.sh']:
        text = (HOME / name).read_text()
        assert 'PLANGRAPH_AUTOOS_ROOT' in text
        assert '$HERE/' in text
    for name in ['apply.ps1', 'apply-capability-overrides.ps1']:
        assert 'PLANGRAPH_AUTOOS_ROOT' in (HOME / name).read_text()


@pytest.mark.parametrize('path', list(HOME.rglob('*.sh')))
def test_shell_syntax(path):
    subprocess.run(['bash', '-n', str(path)], check=True, capture_output=True)


@pytest.mark.parametrize('name', sorted(set(COPIED) - CHANGED))
def test_autoos_drift(name):
    root = os.environ.get('PLANGRAPH_AUTOOS_ROOT')
    if not root:
        pytest.skip('AutoOS checkout not supplied')
    data = subprocess.check_output(['git', '-C', root, 'show',
                                    'ff2f7f97:configuration/omniroute/' + name])
    if name == 'custom-models.json':
        # Commit 1 deliberately scrubbed the vendor documentation domain.
        import re
        data = re.sub(rb'\S+\.com(?= model table)', b'<vendor-model-documentation>', data)
    assert (HOME / name).read_bytes() == data


def test_allowlist():
    row = {'name': 'sample', 'models': ['first', 'second'], 'strategy': 'fallback',
           'private': 'DROP', 'id': 'DROP'}
    assert EXPORT.normalize({'combos': [row]}) == {'combos': [
        {'name': 'sample', 'members': ['first', 'second'], 'strategy': 'fallback'}]}
    with pytest.raises(ValueError):
        EXPORT.normalize([{'name': 'sample', 'members': [{'id': 'DROP'}]}])


def test_export_api(tmp_path):
    token = 'synthetic-' + 'test-token'
    key = tmp_path / 'key'
    key.write_text(token)
    output = tmp_path / 'combos.json'
    state = {'mode': 'ok', 'calls': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state['calls'] += 1
            assert self.path == '/api/combos'
            assert self.headers['Authorization'] == 'Bearer ' + token
            if state['mode'] == 'redirect':
                self.send_response(302)
                self.send_header('Location', '/elsewhere')
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            name = token if state['mode'] == 'echo' else 'sample'
            self.wfile.write(json.dumps({'combos': [{'name': name, 'models': ['a', 'b'],
                                                    'account': 'DROP'}]}).encode())

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    gateway = f'http://127.0.0.1:{server.server_port}'

    def run(*extra, base=gateway):
        result = subprocess.run([sys.executable, str(HOME / 'export-combos.py'),
                                 '--gateway', base, '--key-file', str(key),
                                 '--output', str(output), *extra], capture_output=True, text=True)
        assert token not in result.stdout + result.stderr
        return result.returncode

    try:
        assert run() == 0
        assert json.loads(output.read_text()) == {'combos': [{'name': 'sample', 'members': ['a', 'b']}]}
        assert token not in output.read_text()
        assert run('--check') == 0
        output.write_text('{"combos": []}')
        assert run('--check') == 1
        for mode in ['redirect', 'echo']:
            state['mode'] = mode
            count = state['calls']
            assert run() == 2
            assert state['calls'] == count + 1
        count = state['calls']
        for value in ['', 'two tokens', 'bad\nheader']:
            key.write_text(value)
            assert run() == 2
        key.unlink()
        assert run() == 2
        key.write_text(token)
        assert run(base='http://192.0.2.1') == 2
        assert run(base='http://localhost') == 2
        assert state['calls'] == count
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
