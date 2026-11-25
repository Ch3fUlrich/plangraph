"""deploy/bootstrap.sh --host-ready-timer installs, enables and verifies the host-ready units."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_bootstrap_ctx_watchdog import ROOT, host  # noqa: F401  (the fixture builds the stub PATH)

UNITS = ('pg-host-ready.service', 'pg-host-ready.timer')


@pytest.fixture
def bootstrap(host, monkeypatch):  # noqa: F811
    home, log, run = host
    bins = Path(os.environ['PATH'])
    for name in ('install', 'mkdir', 'cat'):
        (bins / name).symlink_to(shutil.which(name, path=os.defpath))
    (bins / 'systemctl').write_text(f'''#!{sys.executable}
import os, sys
with open(os.environ['CALLS'], 'a') as f:
    f.write('systemctl ' + ' '.join(sys.argv[1:]) + '\\n')
if 'show' in sys.argv and '-p' in sys.argv:
    print(os.environ.get('RESULT', 'success'))
sys.exit(int(os.environ.get('SYSTEMCTL_RC', '0')) if 'start' in sys.argv else 0)
''')
    python = bins / 'python3'
    python.write_text(f'''#!{sys.executable}
import os, sys
with open(os.environ['CALLS'], 'a') as f:
    f.write('python3 ' + ' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1] == '-m':
    sys.exit(int(os.environ.get('CHECK_RC', '0')) if 'host_ready' in sys.argv[2] else 0)
if sys.argv[1].endswith('omnigraph_step.py'):
    sys.exit(0)
os.execv(sys.executable, [sys.executable] + sys.argv[1:])
''')
    (bins / 'systemctl').chmod(0o755)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    # The timer mode stands alone: no ctx-watchdog configuration, no other bootstrap step.
    (home / '.config/plangraph/ctx-watchdog.json').unlink()
    return home, log, run


def test_installs_enables_runs_and_checks_the_records(bootstrap):
    home, log, run = bootstrap
    result = run('--host-ready-timer')
    assert result.returncode == 0, result.stderr
    for unit in UNITS:
        assert (home / '.config/systemd/user' / unit).read_text() == (ROOT / 'deploy/systemd' / unit).read_text()
    calls = [line for line in log.read_text().splitlines() if (line.startswith('systemctl') and 'show-environment' not in line) or 'host_ready' in line]
    assert calls[:3] == ['systemctl --user daemon-reload', 'systemctl --user enable --now pg-host-ready.timer',
                         'systemctl --user start pg-host-ready.service']
    assert calls[3] == 'systemctl --user show pg-host-ready.service -p Result --value'
    assert calls[-1] == 'python3 -m plangraph.spawner.host_ready --check-records'


def test_the_timer_mode_runs_no_other_bootstrap_step(bootstrap):
    home, log, run = bootstrap
    assert run('--host-ready-timer').returncode == 0
    assert not any(word in log.read_text() for word in ('stall_watchdog', 'git ', 'ctx-watchdog'))


def test_nothing_is_installed_without_the_flag(bootstrap):
    home, log, run = bootstrap
    assert run('--skip-ctx-watchdog').returncode == 0
    assert not (home / '.config/systemd/user/pg-host-ready.timer').exists()
    assert 'host_ready' not in log.read_text()


@pytest.mark.parametrize('env,message', [
    ({'RESULT': 'exit-code'}, 'the service did not run successfully'),
    ({'SYSTEMCTL_RC': '1'}, 'the service did not run successfully'),
    ({'CHECK_RC': '1'}, 'a placement host has a missing or stale record'),
])
def test_a_failed_run_or_missing_record_fails_the_install(bootstrap, monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    result = bootstrap[2]('--host-ready-timer')
    assert result.returncode != 0
    assert message in result.stderr


def test_dry_run_names_every_step_and_writes_nothing(bootstrap):
    home, _, run = bootstrap
    result = run('--dry-run', '--host-ready-timer')
    assert result.returncode == 0, result.stderr
    assert '[DRY-RUN] host-ready: would install pg-host-ready.service and .timer' in result.stdout
    assert not (home / '.config/systemd/user/pg-host-ready.timer').exists()
