"""Bounded, injectable host inspection; unavailable data is never zero drift."""
from pathlib import Path
import re
import shlex
import subprocess

from .release import git
from plangraph._loaded_identity import repo_git


def transport(host):
    live = host['repos']['plangraph']['live_dir']
    command = ['git', '-C', live, 'rev-parse', 'HEAD']
    if host['transport'] == 'ssh':
        remote = (subprocess.list2cmdline(command) if host['os'] == 'windows'
                  else shlex.join(command))
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
                   '--', host.get('ssh_target') or host['id'], remote]
    elif host['transport'] != 'local':
        raise ValueError('unsupported transport')
    return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()


def rows(registry, release, query=transport, count=None):
    count = count or (lambda live, target: int(git('rev-list', '--count', f'{live}..{target}')))
    result = []
    for host in registry['hosts']:
        live, behind = 'unknown', 'unknown'
        try:
            sha = query(host)
            if not re.fullmatch('[0-9a-f]{40,64}', sha):
                raise ValueError('invalid sha')
            live = sha
            if release != 'unknown':
                behind = count(live, release)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            behind = 'unknown'
        result.append({'host': host['id'], 'live_sha': live, 'release_sha': release, 'behind': behind})
    return result


def pid_start(pid):
    try:
        return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None
