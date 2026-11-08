"""Bounded, parallel source-directory checks without exposing SSH diagnostics."""
from concurrent.futures import ThreadPoolExecutor

import subprocess

from .remote_ps import MAX_WORKERS, SRC_MISSING, TIMEOUT, configured_hosts, source_check


def check_host(host):
    command = source_check(host)
    if 'wsl_distro' in host:
        command = f"wsl -d {host['wsl_distro']} -- sh -c '{command}'"
    try:
        result = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
             '--', host['target'], command], check=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=TIMEOUT)
        state = {0: 'ok', SRC_MISSING: 'src missing'}.get(result.returncode, 'unreachable')
    except (OSError, subprocess.TimeoutExpired):
        state = 'unreachable'
    return dict(name='ps_host_' + host['name'],
                status='pass' if state == 'ok' else 'fail', detail=state)


def source_checks():
    try:
        hosts = [host for host in configured_hosts()
                 if not host.get('invalid') and ('src' in host or 'win_src' in host)]
    except (OSError, ValueError, KeyError, TypeError):
        return [dict(name='ps_hosts', status='fail', detail='invalid configuration')]
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(hosts))) as pool:
        return list(pool.map(check_host, hosts))
