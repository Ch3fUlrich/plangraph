"""Card-only per-host agent CLI: python -m plangraph.spawner.cli."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import os
import subprocess
import time

_start_supervisor = subprocess.Popen

from .runner import spawn_service as SpawnService
from plangraph.planner.routing import BenchRequired, SpawnRefused


def _supervise():
    payload = json.loads(sys.stdin.readline())
    service = SpawnService(state=payload.pop('state'))
    async def run():
        record = await service.spawn(**payload, wait_seconds=0)
        print(json.dumps(record), flush=True)
        sys.stdout.close()
        if record['run_id'] in service.tasks:
            await service.tasks[record['run_id']]
    asyncio.run(run())


def _card(value):
    return json.loads(Path(value[1:]).read_text(encoding='utf-8') if value.startswith('@') else value)


def main(argv=None):
    if (argv if argv is not None else sys.argv[1:]) == ['--supervise']:
        _supervise()
        return 0
    parser = argparse.ArgumentParser(description='Spawn by card')
    parser.add_argument('--state')
    commands = parser.add_subparsers(dest='verb', required=True)
    for verb in ('route', 'spawn'):
        p = commands.add_parser(verb)
        p.add_argument('--card', type=_card)
        p.add_argument('--require-bench', action='store_true')
        p.add_argument('--bench-db', action='append')
        p.add_argument('--role', choices=('implement', 'review'), default='implement')
        p.add_argument('--writer-family', action='append', default=[])
        p.add_argument('--review-of')
        p.add_argument('--json', action='store_true')
        if verb == 'spawn':
            p.add_argument('prompt')
            p.add_argument('--model')
            p.add_argument('--leg')
            p.add_argument('--override-reason')
            p.add_argument('--dry-run', action='store_true')
            p.add_argument('--wait', type=float, default=0)
            p.add_argument('--no-fallthrough', action='store_true')
    commands.add_parser('ps')
    for verb in ('result', 'status', 'cancel'):
        commands.add_parser(verb).add_argument('run_id')
    args = parser.parse_args(argv)
    service = SpawnService(state=args.state)
    try:
        from .bench_entry import benchmark_options
        options = benchmark_options(args.require_bench, args.bench_db) if args.verb in ('route', 'spawn') else {}
        if args.verb == 'route':
            result = service.route(args.card, args.role, args.writer_family, args.review_of, **options).to_dict()
        elif args.verb == 'spawn':
            payload = dict(card=args.card, prompt=args.prompt, role=args.role,
                writer_families=args.writer_family, review_of=args.review_of, model=args.model,
                leg=args.leg, override_reason=args.override_reason, dry_run=args.dry_run,
                no_fallthrough=args.no_fallthrough, **options)
            # Validate before detaching so refusals are reported synchronously.
            if not args.model and not args.leg:
                if not args.card:
                    raise ValueError('spawn by card; card required')
                service.route(args.card, args.role, args.writer_family, args.review_of, **options)
            if args.dry_run:
                result = asyncio.run(service.spawn(**payload, wait_seconds=None))
            else:
                child = _start_supervisor([sys.executable, '-m', __package__ + '.cli', '--supervise'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, **({'start_new_session': True} if os.name != 'nt' else
                                  {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}))
                child.stdin.write(json.dumps(dict(payload, state=str(service.state))) + '\n')
                child.stdin.close()
                line = child.stdout.readline()
                child.stdout.close()
                if not line:
                    raise ValueError('spawn supervisor failed; check card and override reason')
                result = json.loads(line)
                deadline = time.monotonic() + max(0, args.wait)
                while result['status'] == 'running' and time.monotonic() < deadline:
                    time.sleep(min(.02, max(0, deadline - time.monotonic())))
                    result = service.result(result['run_id'])
        elif args.verb == 'ps':
            result = service.ps()
        elif args.verb == 'cancel':
            result = asyncio.run(service.cancel(args.run_id))
        else:
            result = service.result(args.run_id)
        print(json.dumps(result, indent=2))
        if isinstance(result, dict):
            if 'status' not in result:
                return 0
            code = result.get('exit_code')
            return code if code is not None else (0 if result.get('status') == 'running' else 1)
        return 0
    except (ValueError, KeyError, OSError, SpawnRefused) as exc:
        print(str(exc), file=sys.stderr)
        return 3 if isinstance(exc, BenchRequired) else 2


if __name__ == '__main__':
    raise SystemExit(main())
