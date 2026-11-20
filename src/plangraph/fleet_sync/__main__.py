"""Fleet release, rollout, drift, production checkout checks and HEAD audit."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import advance, drift, genconfig, release


def _host_sync_command():
    for name in ('host-sync', 'plangraph'):
        found = shutil.which(name)
        if found:
            return [found] if name == 'host-sync' else [found, 'host-sync']
    roots = []
    if os.environ.get('CARGO_TARGET_DIR'):
        roots.append(Path(os.environ['CARGO_TARGET_DIR']))
    roots.append(Path(__file__).resolve().parents[3] / 'rust/target')
    for root in roots:
        for profile in ('release', 'debug'):
            for name in ('host-sync', 'plangraph'):
                candidate = root / profile / name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return [str(candidate)] if name == 'host-sync' else [str(candidate), 'host-sync']
    return None


def _run_host_sync(data_dir, bin_dir):
    command = _host_sync_command()
    if command is None:
        print('FAIL host-sync: plangraph host-sync binary not found')
        return 1
    if data_dir:
        command.extend(['--data-dir', data_dir])
    if bin_dir:
        command.extend(['--bin-dir', bin_dir])
    return subprocess.run(command, check=False).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    rel = commands.add_parser('release')
    rel.add_argument('verb', choices=['record', 'show'])
    gen = commands.add_parser('genconfig')
    gen.add_argument('verb', choices=['write', 'check'])
    adv = commands.add_parser('advance')
    adv.add_argument('--sha', required=True)
    adv.add_argument('--hosts', nargs='+', required=True)
    adv.add_argument('--go', required=True)
    adv.add_argument('--dry-run', action='store_true')
    for command in (gen, commands.add_parser('drift'), adv):
        command.add_argument('--registry', type=__import__('pathlib').Path,
                             default=genconfig.config_dir() / 'hosts.json')
    commands.choices['drift'].add_argument('--json', action='store_true')
    for name in ('check-checkout', 'install-audit'):
        command = commands.add_parser(name)
        command.add_argument('--repo', required=True)
        command.add_argument('--state-dir', required=True)
    check = commands.choices['check-checkout']
    check.add_argument('--default-branch')
    commands.choices['install-audit'].add_argument('--apply', action='store_true')
    host_sync = commands.add_parser('host-sync')
    host_sync.add_argument('--data-dir')
    host_sync.add_argument('--bin-dir')
    args = parser.parse_args(argv)
    try:
        if args.command == 'host-sync':
            return _run_host_sync(args.data_dir, args.bin_dir)
        if args.command == 'check-checkout':
            send = drift.alert_sender()
            def notify(line):
                print(line)
                send(line)
            return int(bool(drift.check_checkout(args.repo, notify, state_dir=args.state_dir,
                default_branch=args.default_branch)))
        if args.command == 'install-audit':
            print(json.dumps({'apply': args.apply, 'hooks': drift.install_audit(
                args.repo, args.state_dir, apply=args.apply)}))
        elif args.command == 'release':
            data = release.record() if args.verb == 'record' else json.loads(release.release_path().read_text())
            print(json.dumps(data))
        elif args.command == 'advance':
            return advance.advance(json.loads(args.registry.read_text()), args.sha,
                                   args.hosts, args.go, args.dry_run)
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
    except ValueError as error:
        if args.command == 'advance':
            print(str(error), file=sys.stderr)
            return 2
        print('fleet sync input or query failed', file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        print('fleet sync input or query failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
