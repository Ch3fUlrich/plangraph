"""python3 -m plangraph.fleet_sync {release,genconfig,drift}."""
import argparse
import json
import subprocess
import sys

from . import drift, genconfig, release


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    rel = commands.add_parser('release')
    rel.add_argument('verb', choices=['record', 'show'])
    gen = commands.add_parser('genconfig')
    gen.add_argument('verb', choices=['write', 'check'])
    for command in (gen, commands.add_parser('drift')):
        command.add_argument('--registry', type=__import__('pathlib').Path,
                             default=genconfig.config_dir() / 'hosts.json')
    commands.choices['drift'].add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'release':
            data = release.record() if args.verb == 'record' else json.loads(release.release_path().read_text())
            print(json.dumps(data))
        elif args.command == 'genconfig':
            return genconfig.sync(json.loads(args.registry.read_text()), args.verb == 'write')
        else:
            try:
                sha = json.loads(release.release_path().read_text())['sha']
            except (OSError, ValueError, KeyError):
                sha = 'unknown'
            data = drift.rows(json.loads(args.registry.read_text()), sha)
            print(json.dumps(data) if args.json else '\n'.join(
                f"{r['host']} {r['live_sha']} {r['release_sha']} {r['behind']}" for r in data))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print('fleet sync input or query failed', file=sys.stderr)
        return 0 if args.command == 'drift' else 1


if __name__ == '__main__':
    raise SystemExit(main())
