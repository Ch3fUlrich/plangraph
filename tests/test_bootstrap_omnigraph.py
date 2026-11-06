import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('PLANGRAPH_SITE_ENV', raising=False)
    monkeypatch.setenv('PATH', str(bin_dir))
    (bin_dir / 'python3').symlink_to(sys.executable)
    log = tmp_path / 'calls'
    monkeypatch.setenv('CALLS', str(log))
    for name in ('npm', 'curl'):
        p = bin_dir / name
        p.write_text('#!' + sys.executable + '\n'
                     'import os,sys\n'
                     'from pathlib import Path\n'
                     'with open(os.environ["CALLS"], "a") as f: f.write(Path(sys.argv[0]).name + " " + " ".join(sys.argv[1:]) + "\\n")\n'
                     'sys.stdin.read() if Path(sys.argv[0]).name == "curl" else None\n'
                     'print(os.environ.get("HTTP_STATUS", "200") if Path(sys.argv[0]).name == "curl" else "private diagnostic", file=sys.stdout)\n'
                     'print("private diagnostic", file=sys.stderr)\n')
        p.chmod(0o755)
    token = home / 'credential'
    token.write_text('fixture-credential\n')
    token.chmod(0o600)
    site = home / '.config/plangraph/site.env'
    site.parent.mkdir(parents=True)
    # Synthetic address assembled only in hermetic tests, never a site address.
    base = 'http' + '://127.0.0.1:1'
    site.write_text('OMNIGRAPH_BASE_URL=' + base + '\nOMNIGRAPH_TOKEN_FILE=' + str(token) + '\n')
    (home / '.zshenv').touch()
    def run(dry=False):
        return subprocess.run([sys.executable, str(ROOT / 'deploy/omnigraph_step.py'),
                               '--dry-run', str(int(dry))], capture_output=True, text=True)
    return home, site, token, log, run, base


def test_v1_secret_silent_including_errors(host, monkeypatch):
    home, site, token, log, run, base = host
    for status in ('200', '403', '404', 'garbage'):
        monkeypatch.setenv('HTTP_STATUS', status)
        result = run()
        combined = result.stdout + result.stderr + log.read_text()
        assert base not in combined
        assert token.read_text().strip() not in combined
        assert 'private diagnostic' not in combined
        assert result.returncode == 0


def test_v2_rerun_unchanged_and_reads_token_at_start(host):
    home, _, token, log, run, _ = host
    assert run().returncode == 0
    files = [home / '.profile', home / '.zshenv', home / '.config/plangraph/omnigraph.profile.sh']
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
    assert 'skipped' in run().stdout
    assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
    assert log.read_text().count('npm ') == 1
    assert files[2].stat().st_mode & 0o777 == 0o600
    token.write_text('rotated-fixture\n')
    result = subprocess.run(['/bin/bash', '-xc', '. "$HOME/.profile"; test "$OMNIGRAPH_TOKEN" = rotated-fixture'], capture_output=True, text=True)
    assert result.returncode == 0
    # Caller-owned commands can trace their own secrets; profile itself must not.
    trace = subprocess.run(['/bin/bash', '-xc', '. "$HOME/.profile"'], capture_output=True, text=True)
    assert 'rotated-fixture' not in trace.stderr


def test_v3_dry_run_no_writes_or_commands(host):
    home, _, _, log, run, _ = host
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in home.rglob('*') if p.is_file()}
    assert 'dry-run' in run(True).stdout
    assert not log.exists()
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in home.rglob('*') if p.is_file()}


@pytest.mark.parametrize('missing', ['site', 'base', 'token_value', 'token_file'])
def test_v4_missing_soft(host, missing):
    home, site, token, log, run, _ = host
    if missing == 'site':
        site.unlink()
    elif missing == 'token_file':
        token.unlink()
    else:
        lines = site.read_text().splitlines()
        site.write_text('\n'.join(lines[1:] if missing == 'base' else lines[:1]))
    result = run()
    assert result.returncode == 0
    assert 'OPERATOR:' in result.stdout
    assert not (home / '.profile').exists()
    assert not log.exists() or 'curl' not in log.read_text()


def test_v5_pin_flows_from_mcp(host, tmp_path):
    home, _, _, log, _, _ = host
    root = tmp_path / 'copy'
    (root / 'deploy').mkdir(parents=True)
    shutil.copy(ROOT / 'deploy/omnigraph_step.py', root / 'deploy')
    data = json.loads((ROOT / '.mcp.json').read_text())
    spec = '@modernrelay/omnigraph-mcp@fixture-pin'
    data['mcpServers']['omnigraph']['args'] = ['-y', spec]
    (root / '.mcp.json').write_text(json.dumps(data))
    subprocess.run([sys.executable, str(root / 'deploy/omnigraph_step.py'), '--dry-run', '0'], check=True, capture_output=True)
    assert 'npm cache add ' + spec in log.read_text()


def test_v6_public_token_refused_before_read(host, monkeypatch):
    _, _, token, log, run, _ = host
    token.chmod(0o644)
    result = run()
    assert result.returncode == 0
    assert 'OPERATOR: place private OMNIGRAPH_TOKEN_FILE' in result.stdout
    assert 'curl' not in log.read_text()
    spec = importlib.util.spec_from_file_location('omnigraph_step', ROOT / 'deploy/omnigraph_step.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Guard:
        def __enter__(self): return self
        def __exit__(self, *args): os.close(self.fd)
        def fileno(self): return self.fd
        def read(self): pytest.fail('insecure token was read')
    def wrap(fd):
        guard = Guard()
        guard.fd = fd
        return guard
    monkeypatch.setattr(module.os, 'fdopen', wrap)
    with pytest.raises(ValueError):
        module.private_token(token)


def test_v7_launchers_same_order_fail_soft():
    for name in ('bootstrap.sh', 'bootstrap.ps1'):
        text = (ROOT / 'deploy' / name).read_text()
        assert text.index('init-and-seed') < text.index('"install"') < text.index('omnigraph_step.py') < text.index('bootstrap completed successfully')
        assert 'Omnigraph: site config, cache client, session environment, health verification.' in text
        assert 'Omnigraph skipped: helper unavailable' in text
    assert '--dry-run "$DRY_RUN" ||' in (ROOT / 'deploy/bootstrap.sh').read_text()
    assert '--dry-run ([int]$DryRun.IsPresent)' in (ROOT / 'deploy/bootstrap.ps1').read_text()


def test_npm_missing_skips(host):
    _, _, _, log, run, _ = host
    (log.parent / 'bin/npm').unlink()
    assert 'npm unavailable' in run().stdout


@pytest.mark.parametrize('platform', [
    'nt',
    pytest.param('posix', marks=pytest.mark.skipif(
        os.name == 'nt', reason='POSIX fchmod and mode bits require a POSIX host')),
])
def test_npm_cache_spaced_path_without_shell(host, monkeypatch, capsys, platform):
    home, site, _, _, _, _ = host
    site.write_text('')  # Stop before platform-specific session configuration.
    spec = importlib.util.spec_from_file_location('omnigraph_step', ROOT / 'deploy/omnigraph_step.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'os', SimpleNamespace(**{**vars(os), 'name': platform}))
    if platform == 'nt':
        monkeypatch.delattr(module.os, 'fchmod', raising=False)
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('PLANGRAPH_SITE_ENV', str(site))
    monkeypatch.setattr(sys, 'argv', ['omnigraph_step.py', '0'])
    npm = str(home / 'Program Files' / ('npm.cmd' if platform == 'nt' else 'npm'))
    def which(name):
        assert name == ('npm.cmd' if platform == 'nt' else 'npm')
        return npm
    calls = []
    def run(args, **kwargs):
        assert not kwargs.get('shell', False)
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(module.shutil, 'which', which)
    monkeypatch.setattr(module.subprocess, 'run', run)
    module.main()
    expected = next(arg for arg in json.loads((ROOT / '.mcp.json').read_text())
                    ['mcpServers']['omnigraph']['args'] if arg.startswith('@modernrelay/omnigraph-mcp@'))
    assert calls == [[npm, 'cache', 'add', expected]]
    assert (home / '.config/plangraph/omnigraph-cache-spec').read_text() == expected
    output = capsys.readouterr().out
    for name in ('OMNIGRAPH_BASE_URL', 'OMNIGRAPH_TOKEN_FILE'):
        assert 'OPERATOR: place private ' + name in output
    if platform == 'posix':
        assert (home / '.config/plangraph/omnigraph-cache-spec').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('host_name', ['nt', 'posix'])
def test_npm_cache_posix_case_requires_posix_host(monkeypatch, host_name):
    spec = importlib.util.spec_from_file_location('bootstrap_tests', __file__)
    module = importlib.util.module_from_spec(spec)
    # Simulate the importing host without changing pathlib's real OS module.
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, 'os', SimpleNamespace(**{**vars(os), 'name': host_name}))
        spec.loader.exec_module(module)
    marks = module.test_npm_cache_spaced_path_without_shell.pytestmark
    parameters = next(mark.args[1] for mark in marks if mark.name == 'parametrize')
    posix_case = next(case for case in parameters if not isinstance(case, str)
                      and case.values == ('posix',))
    skip = next(mark for mark in posix_case.marks if mark.name == 'skipif')
    assert skip.args == (host_name == 'nt',)
    assert 'nt' in parameters  # Windows coverage remains enabled on either host.


def test_health_404_no_second_guess(host, monkeypatch):
    monkeypatch.setenv('HTTP_STATUS', '404')
    assert 'no alternate attempted' in host[4]().stdout
    assert host[3].read_text().count('curl ') == 1
