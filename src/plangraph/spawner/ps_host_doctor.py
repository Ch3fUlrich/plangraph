"""Bounded, parallel source-directory checks without exposing SSH diagnostics."""
from concurrent.futures import ThreadPoolExecutor

import subprocess

from .remote_ps import MAX_WORKERS, TIMEOUT, configured_hosts, exit_reason, host_command


def check_host(host):
    try:
        command = host_command(host)
        result = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
             '--', host['target'], command], check=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=TIMEOUT)
        reason = exit_reason(result.returncode)
        state = {None: 'ok', 'host src missing': 'src missing',
                 'host unreachable': 'unreachable'}.get(reason, reason)
    except (ValueError, KeyError, TypeError, AttributeError):
        state = 'invalid configuration'
    except OSError:
        state = 'unreachable'
    except subprocess.TimeoutExpired:
        state = 'host query failed'
    return dict(name='ps_host_' + host['name'],
                status='pass' if state == 'ok' else 'fail', detail=state)


def source_checks():
    try:
        hosts = [host for host in configured_hosts()
                 if not host.get('invalid') and ('src' in host or 'win_src' in host)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return [dict(name='ps_hosts', status='fail', detail='invalid configuration')]
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(hosts))) as pool:
        return list(pool.map(check_host, hosts))
