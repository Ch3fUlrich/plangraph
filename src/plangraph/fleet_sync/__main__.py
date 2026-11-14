"""Fleet release, rollout, drift, production checkout checks and HEAD audit."""
import argparse
import json
import subprocess
import sys

from . import advance, drift, genconfig, release


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
    for name in ('check-checkout', 'install-audit', 'head-move'):
        command = commands.add_parser(name)
        command.add_argument('--repo', required=True)
        if name != 'check-checkout':
            command.add_argument('--state-dir', required=True)
    check = commands.choices['check-checkout']
    check.add_argument('--default-branch')
    check.add_argument('--routing', default='routing')
    commands.choices['install-audit'].add_argument('--apply', action='store_true')
    audit = commands.choices['head-move']
    audit.add_argument('--hook', required=True, choices=['post-checkout', 'reference-transaction'])
    audit.add_argument('hook_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.command == 'check-checkout':
            from plangraph.fleet_bus.client import build_client
            def send(recipient, line):
                print(line)
                build_client().send(recipient, line)
            return int(bool(drift.check_checkout(args.repo, send,
                default_branch=args.default_branch, routing=args.routing)))
        if args.command == 'install-audit':
            print(json.dumps({'apply': args.apply, 'hooks': drift.install_audit(
                args.repo, args.state_dir, apply=args.apply)}))
        elif args.command == 'head-move':
            hook_args = args.hook_args
            if hook_args[:1] == ['--']:
                hook_args = hook_args[1:]
            drift.audit_head(args.repo, args.state_dir, args.hook, hook_args,
                             sys.stdin.read() if args.hook == 'reference-transaction' else '')
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
    except (OSError, KeyError, TypeError, subprocess.SubprocessError):
        print('fleet sync input or query failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
