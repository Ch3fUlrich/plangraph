"""Pull durable host findings with at-least-once notification delivery."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import uuid

from .private_io import atomic_write_private


def exchange(acks):
    from .stall_watchdog import _read_state
    state = Path(os.environ.get('PLANGRAPH_SPAWN_STATE',
                 Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'plangraph'))
    out = state / 'watchdog/outbox'
    for key in acks:
        try:
            if isinstance(key, str) and len(key) == 64 and all(c in '0123456789abcdef' for c in key):
                (out / (key + '.json')).unlink(missing_ok=True)
        except OSError:
            continue
    return {p.stem: r for p in sorted(out.glob('*.json'))
            if (r := _read_state(state, p)) is not None}


def poll(hosts, command, state):
    path = Path(state) / 'remote-probe.json'
    try:
        saved = json.loads(path.read_text())
        if not isinstance(saved, dict):
            raise TypeError('invalid probe state')
    except (FileNotFoundError, ValueError, TypeError):
        saved = {}
    def save():
        atomic_write_private(path, json.dumps(saved))
    def notify(key, event, entry):
        if key not in entry['seen']:
            event = dict(event, event_id=event.get('event_id') or key)
            subprocess.run(shlex.split(command), input=json.dumps(event, sort_keys=True) + '\n',
                           text=True, check=True, timeout=20)
            entry['seen'].append(key)
            save()
    def flush(entry):
        for key, event in list(entry.setdefault('pending', {}).items()):
            try:
                notify(key, event, entry)
                del entry['pending'][key]
                save()
            except (OSError, subprocess.SubprocessError):
                continue
    for host in hosts:
        entry = saved.setdefault(host, dict(failures=0, outage=0, down=False, seen=[], acks=[]))
        try:
            if (any(type(entry[k]) is not int or entry[k] < 0 for k in ('failures', 'outage'))
                    or type(entry['down']) is not bool
                    or any(not isinstance(entry[k], list) or any(not isinstance(v, str)
                           for v in entry[k]) for k in ('seen', 'acks'))):
                raise ValueError('invalid probe entry')
        except (ValueError, TypeError, KeyError):
            entry = saved[host] = dict(failures=0, outage=0, down=False, seen=[], acks=[])
        pending = entry.setdefault('pending', {})
        try:
            code = ('import json; from plangraph.spawner.remote_probe import exchange; '
                    'print(json.dumps(exchange(' + repr(entry['acks']) + ')))')
            reply = subprocess.run(['ssh', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes',
                                    '--', host, 'cd ' + shlex.quote(os.environ['PLANGRAPH_PROBE_ROOT'])
                                    + ' && PYTHONPATH=src python3 -c ' + shlex.quote(code)],
                                   capture_output=True, text=True, timeout=30, check=True)
            events = json.loads(reply.stdout)
            if not isinstance(events, dict) or any(
                    len(k) != 64 or any(c not in '0123456789abcdef' for c in k)
                    or not isinstance(v, dict) for k, v in events.items()):
                raise ValueError('invalid remote findings')
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            entry['failures'] += 1
            if entry['failures'] >= 2:
                if not entry['down']:
                    entry['outage'] += 1
                    entry['episode'] = uuid.uuid4().hex
                    entry['down'] = True
                    pending['down-' + entry['episode']] = dict(rule='host-unreachable', host=host)
            save()
            flush(entry)
            continue
        if entry['down']:
            pending['up-' + entry.get('episode', str(entry['outage']))] = dict(rule='host-recovered', host=host)
        entry.update(failures=0, down=False)
        save()
        flush(entry)
        entry['acks'] = []
        for key, event in events.items():
            try:
                notify(key, event, entry)
                entry['acks'].append(key)
                save()
            except (OSError, subprocess.SubprocessError):
                continue
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', action='append')
    parser.add_argument('--notify-cmd', default=os.environ.get('PLANGRAPH_PROBE_NOTIFY_CMD'))
    args = parser.parse_args()
    if not os.environ.get('PLANGRAPH_PROBE_ROOT'):
        parser.error('PLANGRAPH_PROBE_ROOT is required')
    hosts = args.host or shlex.split(os.environ.get('PLANGRAPH_PROBE_HOSTS', ''))
    if not hosts or not args.notify_cmd or any(h.startswith('-') for h in hosts):
        parser.error('hosts and notify command are required')
    state = Path(os.environ.get('PLANGRAPH_SPAWN_STATE',
                 Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'plangraph'))
    poll(hosts, args.notify_cmd, state)


if __name__ == '__main__':
    main()
