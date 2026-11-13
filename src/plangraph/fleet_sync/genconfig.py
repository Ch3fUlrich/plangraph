"""Generate the existing SSH consumers' formats, without enabling public overflow."""
import json
import os
import re
from pathlib import Path

from plangraph.routing_support.private_io import atomic_write_private


def config_dir():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'plangraph'


def generate(registry):
    ps, overflow = [], {}
    for host in registry['hosts']:
        if host['transport'] == 'local':
            continue
        repo = host['repos'].get('plangraph')
        if repo is None:
            continue
        if host['transport'] != 'ssh':
            raise ValueError('config consumers require ssh transport')
        live = repo.get('live_dir')
        if not live:
            raise ValueError('plangraph.live_dir is required for generated hosts')
        name = host['id']
        pattern = (r'[A-Za-z]:[/\\][A-Za-z0-9_./\\-]*' if host['os'] == 'windows'
                   else r'/[A-Za-z0-9_./-]*')
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', name) or not re.fullmatch(pattern, live):
            raise ValueError('invalid host id or live directory')
        src = live.rstrip('/\\') + '/src'
        target = host.get('ssh_target') or name
        ps.append(dict(name=name, target=target, **{
            'win_src' if host['os'] == 'windows' else 'src': src}))
        if host['os'] == 'linux':
            overflow[name] = {'ssh': target, 'repo': live, 'spawner_src': src,
                              'floor_mb': host['caps']['mem_floor_mb']}
    return {'ps-hosts.json': {'hosts': ps},
            'overflow-hosts.json': {'hosts': overflow, 'public_repos': []}}


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
