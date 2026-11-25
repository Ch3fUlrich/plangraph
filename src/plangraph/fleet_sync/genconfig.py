"""Derive SSH routes and public repository paths from the host registry."""
import json
import os
import re
from pathlib import Path

from plangraph.routing_support.private_io import atomic_write_private


# A registry without a `public_repos` key lists this public repository; an explicit list,
# even an empty one, replaces the default.
DEFAULT_PUBLIC_REPOS = ('plangraph',)


def config_dir():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'plangraph'


def work_mounts(host):
    value = host.get('env', {}).get('PLANGRAPH_WORK_MOUNTS')
    return value.split(':') if value else []


def generate(registry):
    ps, overflow = [], {}
    local_name = registry.get('self', '').lower()
    public_ids = set(registry['public_repos'] if 'public_repos' in registry
                     else DEFAULT_PUBLIC_REPOS)
    public = {}
    for host in registry['hosts']:
        name = host['id']
        repos = host.get('repos', {})
        if local_name and local_name in {v.lower() for v in [name, *host.get('aliases', [])]}:
            public.update({key: repo['root'] for key, repo in repos.items() if key in public_ids})
        if host['transport'] == 'local':
            continue
        repo = repos.get('plangraph')
        if repo is None:
            continue
        if host['transport'] != 'ssh':
            raise ValueError('config consumers require ssh transport')
        root = repo['root']
        windows = host['os'] == 'windows'
        pattern = r'[A-Za-z]:[/\\][A-Za-z0-9_ ./\\-]*' if windows else r'/[A-Za-z0-9_ ./-]*'
        if (not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', name)
                or not isinstance(root, str) or not re.fullmatch(pattern, root)):
            raise ValueError('invalid host id or checkout directory')
        target = host.get('ssh_target') or name
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_@.:-]*', target):
            raise ValueError('invalid SSH destination')
        distro = host.get('wsl_distro')
        if distro is not None and (windows
                or not re.fullmatch(r'[A-Za-z0-9_.][A-Za-z0-9_.-]*', distro)):
            raise ValueError('invalid WSL distribution')
        src = root.rstrip('/\\') + '/src'
        hop = {'wsl_distro': distro} if distro else {}
        state = host.get('state_dir')  # `ps` reads the spawner state the host's runs are written to
        # A malformed value is carried on so the consumer rejects the host, not the default store.
        ps.append(dict(name=name, target=target, **hop, **{'win_src' if windows else 'src': src},
                       **({} if state in (None, '') else {'state_dir': state})))
        if not windows:
            overflow[name] = dict(ssh=target, repo=root, spawner_src=src,
                floor_mb=host['caps']['mem_floor_mb'], **hop)
            if 'roles' in host:
                overflow[name].update(roles=host['roles'], state_dir=host['state_dir'],
                    placement_fallback=host.get('placement_fallback', False),
                    repos={key: entry['root'] for key, entry in repos.items() if key in public_ids},
                    session_unit=host.get('env', {}).get('PG_SESSION_UNIT'), registry=True,
                    remote_placement=host.get('remote_placement') is True)
            if mounts := work_mounts(host):
                overflow[name]['work_mounts'] = mounts
    return {'ps-hosts.json': {'hosts': ps}, 'overflow-hosts.json': {
        'hosts': overflow, 'public_repos': list(public.values()), **(
            {'public_repo_ids': public} if public else {})}}


def sync(registry, write=False, directory=None):
    directory = directory or config_dir()
    changed = []
    for name, data in generate(registry).items():
        path = directory / name
        text = json.dumps(data, indent=2, sort_keys=True) + '\n'
        try:
            matches = json.loads(path.read_text()) == data
        except (OSError, ValueError):
            matches = False
        if not matches:
            changed.append(name)
            if write:
                atomic_write_private(path, text)
    print(f'{len(changed)} files differ' + (': ' + ', '.join(changed) if changed else ''))
    return 0 if write or not changed else 1
