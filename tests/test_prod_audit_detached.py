"""A stuck audit supervisor must not keep either HEAD observer or Git waiting."""
import os
import signal
import subprocess
import sys
import time

import pytest

from plangraph.fleet_sync import drift


@pytest.fixture
def audit_checkout(tmp_path):
    repo, state = tmp_path / 'repo', tmp_path / 'state'
    repo.mkdir()
    git = ['/usr/bin/git', '-C', str(repo)]
    subprocess.run([*git, 'init', '-b', 'main'], check=True, capture_output=True)
    subprocess.run([*git, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '--allow-empty', '-m', 'one'], check=True, capture_output=True)
    sha = subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()
    drift.install_audit(repo, state, apply=True)
    return repo, state, git, sha


@pytest.mark.parametrize('invocation', ['post-checkout', 'reference-transaction', 'git'])
def test_unresponsive_audit_does_not_hold_hook_or_git(audit_checkout, tmp_path, invocation):
    repo, state, git, sha = audit_checkout
    binpath = tmp_path / 'bin'
    binpath.mkdir()
    pidfile = tmp_path / 'audit-pid'
    # Model timeout stuck waiting on an unkillable child. Catchable signals
    # cannot stop this supervisor; only the test's final cleanup uses SIGKILL.
    timeout = binpath / 'timeout'
    timeout.write_text(f'#!{sys.executable}\nimport os, signal, time\n'
                       'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                       'signal.signal(signal.SIGALRM, signal.SIG_IGN)\n'
                       f'with open({str(pidfile) + ".tmp"!r}, "w") as f:\n'
                       ' f.write(f"{os.getpid()} {os.getsid(0)}")\n'
                       f'os.replace({str(pidfile) + ".tmp"!r}, {str(pidfile)!r})\n'
                       'print("audit stdout", flush=True)\n'
                       'print("audit stderr", file=__import__("sys").stderr, flush=True)\n'
                       'while True: time.sleep(60)\n')
    timeout.chmod(0o755)
    env = dict(os.environ, PATH=str(binpath) + os.pathsep + os.defpath)
    if invocation == 'git':
        command, stdin = [*git, 'checkout', '--detach', sha], ''
    else:
        hook = str(repo / '.git/hooks' / invocation)
        command, stdin = ([hook, sha, sha, '1'], '') if invocation == 'post-checkout' else (
            [hook, 'committed'], f'{sha} {sha} HEAD\n')
    process = subprocess.Popen(command, cwd=repo, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(stdin, timeout=0.5)
        assert process.returncode == 0, stderr
        assert 'audit stdout' not in stdout
        assert 'audit stderr' not in stderr
        deadline = time.monotonic() + 2
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        pid, session = map(int, pidfile.read_text().split())
        assert session != process.pid, 'audit must have its own session'
        os.kill(pid, signal.SIGTERM)
        os.kill(pid, 0)  # still running after the hook/Git has returned
        assert subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip() == sha
    finally:
        # The failing foreground implementation leaves the hook and supervisor
        # in this group; the detached implementation needs separate cleanup.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=2)
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text().split()[0]), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_detached_runtime_still_has_external_deadline(audit_checkout):
    from pathlib import Path

    repo, state, git, sha = audit_checkout
    pidfile = state / 'runtime-pid'
    (state / 'head-audit.py').write_text(
        'import os, signal, time\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        'signal.signal(signal.SIGALRM, signal.SIG_IGN)\n'
        f'with open({str(pidfile) + ".tmp"!r}, "w") as f: f.write(str(os.getpid()))\n'
        f'os.replace({str(pidfile) + ".tmp"!r}, {str(pidfile)!r})\n'
        'while True: time.sleep(60)\n')
    subprocess.run([*git, 'checkout', '--detach', sha], check=True, capture_output=True, timeout=0.5)
    pid = None
    try:
        deadline = time.monotonic() + 2
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        pid = int(pidfile.read_text())
        status = Path(f'/proc/{pid}/status')
        while time.monotonic() < deadline:
            if not status.exists() or '\nState:\tZ' in status.read_text():
                break
            time.sleep(0.01)
        else:
            pytest.fail('detached runtime survived the external one-second deadline')
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('bounded', [True, False, 'detached'])
def test_installer_upgrades_prior_foreground_stubs(audit_checkout, bounded):
    repo, state, _, _ = audit_checkout
    for name in ('post-checkout', 'reference-transaction'):
        hook = repo / '.git/hooks' / name
        content = hook.read_text()
        # Reconstruct installed stubs from before ancestor snapshots were added.
        prior = content[:content.index('# Capture ancestors')] + content[content.index('setsid timeout '):]
        prior = prior.replace(' "$@" </dev/null', ' </dev/null')
        foreground = prior.replace(
            'command -v setsid >/dev/null 2>&1 || exit 0\n', '').replace(
            'setsid timeout ', 'timeout ', 1).replace(' &\nexit 0\n', ' || true\nexit 0\n')
        if not bounded:
            foreground = foreground.replace('command -v timeout >/dev/null 2>&1 || exit 0\n', '').replace(
                'timeout -s KILL 1 ', '', 1)
        hook.write_text(prior if bounded == 'detached' else foreground)
    drift.install_audit(repo, state, apply=True)
    drift.install_audit(repo, state, apply=True)
    for name in ('post-checkout', 'reference-transaction'):
        content = (repo / '.git/hooks' / name).read_text()
        assert 'setsid timeout -s KILL 1 ' in content
        assert '2>&1 &\nexit 0\n' in content
