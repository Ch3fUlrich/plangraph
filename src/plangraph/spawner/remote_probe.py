"""Pull durable host findings; retain remote data until local notification succeeds."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess

from .private_io import atomic_write_private


def exchange(acks):
    from .stall_watchdog import _read_state
    state = Path(os.environ.get('PLANGRAPH_SPAWN_STATE',
                 Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'plangraph'))
    out = state / 'watchdog/outbox'
    for key in acks:
        if len(key) == 64 and all(c in '0123456789abcdef' for c in key):
            (out / (key + '.json')).unlink(missing_ok=True)
    return {p.stem: r for p in sorted(out.glob('*.json'))
            if (r := _read_state(state, p)) is not None}


def poll(hosts, command, state):
    path = Path(state) / 'remote-probe.json'
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError:
        saved = {}
    def save():
        atomic_write_private(path, json.dumps(saved))
    def notify(key, event, entry):
        if key not in entry['seen']:
            subprocess.run(shlex.split(command), input=json.dumps(event, sort_keys=True) + '\n',
                           text=True, check=True, timeout=20)
            entry['seen'].append(key)
            save()
    for host in hosts:
        entry = saved.setdefault(host, dict(failures=0, outage=0, down=False, seen=[], acks=[]))
        try:
            code = ('import json; from plangraph.spawner.remote_probe import exchange; '
                    'print(json.dumps(exchange(' + repr(entry['acks']) + ')))')
            reply = subprocess.run(['ssh', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes',
                                    '--', host, 'python3 -c ' + shlex.quote(code)],
                                   capture_output=True, text=True, timeout=30, check=True)
            events = json.loads(reply.stdout)
            if not isinstance(events, dict) or any(
                    len(k) != 64 or any(c not in '0123456789abcdef' for c in k)
                    or not isinstance(v, dict) for k, v in events.items()):
                raise ValueError('invalid remote findings')
        except (OSError, ValueError, subprocess.SubprocessError):
            entry['failures'] += 1
            if entry['failures'] >= 2:
                if not entry['down']:
                    entry['outage'] += 1
                    entry['down'] = True
                save()
                try:
                    notify('down-' + str(entry['outage']), dict(rule='host-unreachable', host=host), entry)
                except (OSError, subprocess.SubprocessError):
                    pass
            save()
            continue
        try:
            if entry['down']:
                notify('up-' + str(entry['outage']), dict(rule='host-recovered', host=host), entry)
            entry.update(failures=0, down=False, acks=[])
            for key, event in events.items():
                notify(key, event, entry)
                entry['acks'].append(key)
                save()
        except (OSError, subprocess.SubprocessError):
            pass
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', action='append')
    parser.add_argument('--notify-cmd', default=os.environ.get('PLANGRAPH_PROBE_NOTIFY_CMD'))
    args = parser.parse_args()
    hosts = args.host or shlex.split(os.environ.get('PLANGRAPH_PROBE_HOSTS', ''))
    if not hosts or not args.notify_cmd or any(h.startswith('-') for h in hosts):
        parser.error('hosts and notify command are required')
    state = Path(os.environ.get('PLANGRAPH_SPAWN_STATE',
                 Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'plangraph'))
    poll(hosts, args.notify_cmd, state)


if __name__ == '__main__':
    main()
