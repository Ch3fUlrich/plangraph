"""Card-only per-host agent CLI: python -m plangraph.spawner.cli."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from .service import SpawnService


def _card(value):
    return json.loads(Path(value[1:]).read_text(encoding='utf-8') if value.startswith('@') else value)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Spawn by card')
    parser.add_argument('--state')
    commands = parser.add_subparsers(dest='verb', required=True)
    for verb in ('route', 'spawn'):
        p = commands.add_parser(verb)
        p.add_argument('--card', type=_card)
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
        if args.verb == 'route':
            result = service.route(args.card, args.role, args.writer_family, args.review_of).to_dict()
        elif args.verb == 'spawn':
            # Keep the event loop alive through execution; MCP supports asynchronous return.
            result = asyncio.run(service.spawn(card=args.card, prompt=args.prompt, role=args.role,
                writer_families=args.writer_family, review_of=args.review_of, model=args.model,
                leg=args.leg, override_reason=args.override_reason, dry_run=args.dry_run,
                wait_seconds=None, no_fallthrough=args.no_fallthrough))
        elif args.verb == 'ps':
            result = service.ps()
        elif args.verb == 'cancel':
            result = asyncio.run(service.cancel(args.run_id))
        else:
            result = service.result(args.run_id)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, KeyError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
