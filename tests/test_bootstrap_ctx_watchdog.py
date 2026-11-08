import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host(tmp_path, monkeypatch):
    repo = tmp_path / 'router-source'
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.org', 'commit',
                    '--allow-empty', '-m', 'Fixture'], check=True, capture_output=True)
    sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    home = tmp_path / 'home'
    home.mkdir()
    bins = tmp_path / 'bin'
    bins.mkdir()
    log = tmp_path / 'calls'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('PATH', str(bins))
    monkeypatch.setenv('CALLS', str(log))
    monkeypatch.delenv('CTX_WATCHDOG_ROUTER_DIR', raising=False)
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    config = home / '.config/plangraph/ctx-watchdog.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps(dict(router_repo=repo.as_uri(),
                                     router_sha=sha, router_dir='custom/router')))
    monkeypatch.delenv('CTX_WATCHDOG_CONFIG', raising=False)
    for name in ('dirname', 'uname', 'timeout'):
        (bins / name).symlink_to(shutil.which(name, path=os.defpath))
    python = bins / 'python3'
    python.write_text(f'#!{sys.executable}\nimport os,sys\n'
                      'if sys.argv[1] == "-m" or sys.argv[1].endswith("omnigraph_step.py"): sys.exit(0)\n'
                      'os.execv(sys.executable, [sys.executable] + sys.argv[1:])\n')
    python.chmod(0o755)
    stub = f'#!{sys.executable}\n' + '''import os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as f:
    f.write(name + ' ' + ' '.join(args) + '\\n')
if name == 'git':
    assert os.environ.get('GIT_TERMINAL_PROMPT') == '0' or 'checkout' in args
    if os.environ.get('FETCH_FAIL') and ('clone' in args or 'fetch' in args):
        print('first diagnostic', file=sys.stderr)
        print(os.environ.get('GIT_ERROR', 'fatal: denied https://user:pass@example.org/router'), file=sys.stderr)
        sys.exit(1)
    if 'rev-parse' in args: sys.exit(0 if (Path(args[1]) / '.git').exists() else 1)
    if os.environ.get('CHECKOUT_FAIL') and 'checkout' in args: sys.exit(1)
    if 'clone' in args:
        (Path(args[-1]) / '.git').mkdir(parents=True)
        exe = Path(args[-1]) / 'router/bin/ctx-watchdog'
        exe.parent.mkdir(parents=True)
        exe.write_text('#!' + sys.executable + '\\nimport os,sys\\nsys.exit(int(os.environ.get("HEALTH_FAIL", "0")))\\n')
        exe.chmod(0o755)
if name == 'systemctl' and os.environ.get('DISABLED') and 'is-enabled' in args: sys.exit(1)
if name == 'systemctl' and 'show' in args:
    if os.environ.get('WRONG_EXEC'): print('/wrong/ctx-watchdog')
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
        service = (base / 'systemd/user/ctx-watchdog.service').read_text()
        import re, shlex
        command = next(line[10:] for line in service.splitlines() if line.startswith('ExecStart='))
        binary = shlex.split(command)[0]
        binary = re.sub(r'%%|%h', lambda m: '%' if m[0] == '%%' else str(Path.home()), binary)
        print('{ path=' + binary + ' ; argv[]=' + binary + ' --apply ; ignore_errors=no ; }')
'''
    for name in ('git', 'systemctl'):
        path = bins / name
        path.write_text(stub)
        path.chmod(0o755)

    def run(*args):
        return subprocess.run(['/bin/bash', str(ROOT / 'deploy/bootstrap.sh'),
                               '--skip-autoos', *args], capture_output=True, text=True)
    return home, log, run


@pytest.mark.parametrize('override', [None, 'alternate/router with spaces', 'literal%h/router'])
def test_install_pinned_host_neutral(host, monkeypatch, override):
    home, log, run = host
    if override:
        monkeypatch.setenv('CTX_WATCHDOG_ROUTER_DIR', override)
    directory = override or 'custom/router'
    result = run()
    assert result.returncode == 0, result.stderr
    units = home / '.config/systemd/user'
    service = (units / 'ctx-watchdog.service').read_text()
    assert f'ExecStart="%h/{directory.replace(chr(37), chr(37) * 2)}/router/bin/ctx-watchdog" --apply' in service
    assert str(home) not in service
    assert 'PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin' in service
    timer = (units / 'ctx-watchdog.timer').read_text()
    for setting in ('OnStartupSec=3min', 'OnUnitActiveSec=5min', 'AccuracySec=30s', 'WantedBy=timers.target'):
        assert setting in timer
    config = json.loads((home / '.config/plangraph/ctx-watchdog.json').read_text())
    assert f'checkout --detach {config["router_sha"]} --' in log.read_text()
    assert 'systemctl --user enable --now ctx-watchdog.timer' in log.read_text()
    assert run().returncode == 0
    assert f'fetch --quiet -- {config["router_repo"]} {config["router_sha"]}' in log.read_text()


@pytest.mark.parametrize('existing', [False, True])
def test_remote_failure(host, monkeypatch, existing):
    home, _, run = host
    if existing:
        (home / 'custom/router/.git').mkdir(parents=True)
    monkeypatch.setenv('FETCH_FAIL', '1')
    result = run()
    assert result.returncode != 0
    assert 'ctx-watchdog: cannot fetch the router repo on this host; this host needs an operator access step' in result.stderr
    assert 'fatal: denied https://example.org/router' in result.stderr
    assert 'user:pass@' not in result.stderr
    assert 'first diagnostic' not in result.stderr


@pytest.mark.parametrize('detail', [
    'https://TOKEN@example.org/router',
    'https://user:[REDACTED]@example.org/router',
    'https://user:pa/ss@word@example.org/router',
    '',
])
def test_redact_git_diagnostics(host, monkeypatch, detail):
    monkeypatch.setenv('FETCH_FAIL', '1')
    monkeypatch.setenv('GIT_ERROR', detail)
    result = host[2]()
    assert result.returncode == 1
    expected = ': https://example.org/router' if detail else ''
    assert result.stderr.endswith('operator access step' + expected + '\n')


@pytest.mark.parametrize('repo', ['ext::command', '-option', 'ssh://-oProxyCommand=command/repo', 'http://example.org/repo', 'relative/repo'])
def test_reject_unsafe_transport(host, repo):
    home, log, run = host
    config = home / '.config/plangraph/ctx-watchdog.json'
    values = json.loads(config.read_text())
    values['router_repo'] = repo
    config.write_text(json.dumps(values))
    result = run()
    assert result.returncode == 1
    assert 'ctx-watchdog: router_repo' in result.stderr
    assert not log.exists() or 'git ' not in log.read_text()


@pytest.mark.parametrize('repo', ['https://example.org/router', 'ssh://git@example.org/router',
                                 'git@example.org:router', '/srv/router'])
def test_allow_safe_transport(host, repo):
    home, _, run = host
    config = home / '.config/plangraph/ctx-watchdog.json'
    values = json.loads(config.read_text())
    values['router_repo'] = repo
    config.write_text(json.dumps(values))
    assert run().returncode == 0


@pytest.mark.parametrize('failure', ['CHECKOUT_FAIL', 'WRONG_EXEC', 'missing_git', 'not_repo'])
def test_install_diagnostics(host, monkeypatch, failure):
    home, log, run = host
    directory = home / 'custom/router'
    if failure == 'missing_git':
        (log.parent / 'bin/git').unlink()
        expected = 'ctx-watchdog: git not found'
    elif failure == 'not_repo':
        directory.mkdir(parents=True)
        expected = f'ctx-watchdog: {directory} exists but is not a git checkout'
    else:
        monkeypatch.setenv(failure, '1')
        expected = (f'ctx-watchdog: cannot check out the pinned commit in {directory}'
                    if failure == 'CHECKOUT_FAIL' else
                    'ctx-watchdog: unhealthy (unit ExecStart does not point at the checkout)')
    result = run()
    assert result.returncode == 1
    assert expected in result.stderr


@pytest.mark.parametrize('field', ['router_repo', 'router_dir'])
def test_reject_git_options(host, field):
    home, log, run = host
    config = home / '.config/plangraph/ctx-watchdog.json'
    values = json.loads(config.read_text())
    values[field] = '--upload-pack=unexpected'
    config.write_text(json.dumps(values))
    result = run()
    assert result.returncode == 1
    assert f'ctx-watchdog: {field} must not start with -' in result.stderr
    assert not log.exists() or 'git ' not in log.read_text()


@pytest.mark.parametrize('failure', ['HEALTH_FAIL', 'DISABLED'])
def test_unhealthy(host, monkeypatch, failure):
    monkeypatch.setenv(failure, '1')
    result = host[2]()
    assert result.returncode != 0
    assert 'ctx-watchdog: unhealthy' in result.stderr


@pytest.mark.parametrize('flag', ['--dry-run', '--skip-ctx-watchdog'])
def test_no_changes(host, flag):
    home, log, run = host
    assert run(flag).returncode == 0
    assert not (home / 'custom').exists()
    assert not (home / '.config/systemd').exists()
    assert not log.exists()


def test_linux_without_systemctl_fails(host):
    _, log, run = host
    (log.parent / 'bin/systemctl').unlink()
    result = run()
    assert result.returncode != 0
    assert 'requires working systemd --user' in result.stderr


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('invalid', ['missing', 'placeholders'])
def test_site_config_required(host, invalid, dry_run):
    home, log, run = host
    config = home / '.config/plangraph/ctx-watchdog.json'
    if invalid == 'missing':
        config.unlink()
    else:
        shutil.copyfile(ROOT / 'deploy/ctx-watchdog/config.example.json', config)
    result = run(*(['--dry-run'] if dry_run else []))
    assert (result.returncode == 0) == dry_run
    assert f'copy deploy/ctx-watchdog/config.example.json to {config} and fill it in' in result.stderr
    assert not log.exists()


@pytest.mark.parametrize('location', ['override', 'xdg'])
def test_site_config_location(host, monkeypatch, location):
    home, _, run = host
    config = home / '.config/plangraph/ctx-watchdog.json'
    target = home / 'site/plangraph/ctx-watchdog.json'
    target.parent.mkdir(parents=True)
    config.rename(target)
    if location == 'override':
        monkeypatch.setenv('CTX_WATCHDOG_CONFIG', str(target))
    else:
        monkeypatch.setenv('XDG_CONFIG_HOME', str(home / 'site'))
    assert run().returncode == 0


@pytest.mark.parametrize('directory', ['.', 'linked'])
def test_resolved_exec_start(host, monkeypatch, directory):
    home, _, run = host
    if directory == 'linked':
        alias = home.parent / 'home-link'
        alias.symlink_to(home, target_is_directory=True)
        monkeypatch.setenv('HOME', str(alias))
    else:
        (home / '.git').mkdir()
        executable = home / 'router/bin/ctx-watchdog'
        executable.parent.mkdir(parents=True)
        executable.write_text('#!/bin/bash\nexit 0\n')
        executable.chmod(0o755)
        monkeypatch.setenv('CTX_WATCHDOG_ROUTER_DIR', str(home))
    result = run()
    assert result.returncode == 0, result.stderr


def test_file_option_rejected(host):
    home, _, run = host
    config = home / '.config/plangraph/ctx-watchdog.json'
    values = json.loads(config.read_text())
    values['router_repo'] = 'file://-oProxyCommand=x/p'
    config.write_text(json.dumps(values))
    assert run().returncode == 1


def test_scp_redaction(host, monkeypatch):
    monkeypatch.setenv('FETCH_FAIL', '1')
    monkeypatch.setenv('GIT_ERROR', 'fatal: TOKEN@example.org:router')
    result = host[2]()
    assert 'TOKEN' not in result.stderr
    assert 'example.org:router' in result.stderr


@pytest.mark.parametrize('override', [None, 'custom-ssh'])
def test_git_noninteractive_and_bounded(host, monkeypatch, override):
    _, log, run = host
    git = log.parent / 'bin/git'
    text = git.read_text().replace("if name == 'git':", "if name == 'git':\n    assert os.environ.get('GIT_SSH_COMMAND') == " + repr(override or 'ssh -o BatchMode=yes'))
    git.write_text(text)
    if override:
        monkeypatch.setenv('GIT_SSH_COMMAND', override)
    else:
        monkeypatch.delenv('GIT_SSH_COMMAND', raising=False)
    timeout = log.parent / 'bin/timeout'
    timeout.unlink()
    timeout.write_text(f'#!{sys.executable}\nimport os, sys\n'
                       'with open(os.environ["CALLS"], "a") as f: f.write("timeout " + " ".join(sys.argv[1:]) + "\\n")\n'
                       'args = sys.argv[2:] if sys.argv[1] == "120" else sys.argv[3:]\n'
                       'os.execvp(args[0], args)\n')
    timeout.chmod(0o755)
    result = run()
    assert result.returncode == 0, result.stderr
    assert 'timeout 120 git clone' in log.read_text()
    assert run().returncode == 0
    assert 'timeout 120 git -C' in log.read_text()
