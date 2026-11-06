"""Advisory frozen-plan review CLI; no automatic plan edits or approvals."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import difflib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import plan_loop_bundle as bundle
from tools import plan_loop_ledger as ledger
from tools import plan_loop_report as reporting
from tools import plan_loop_seats as seats
from tools import plan_loop_spawn as spawning
from tools import plan_loop_stop as stop
from plangraph.spawner.fact_check import parse_report


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def atomic_text(path, text):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.state-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save(directory, manifest, rows, yields):
    # Journal the projections first so interruption can be recovered on load.
    manifest['driver_projection'] = {'ledger': rows, 'yields': yields}
    ledger.atomic_json(directory / 'manifest.json', manifest)
    ledger.atomic_json(directory / 'ledger.json', rows)
    atomic_text(directory / 'yield.jsonl', ''.join(json.dumps(r) + '\n' for r in yields))


def load(directory):
    policy = seats.load_policy(directory / 'plan-loop.toml')
    manifest = read(directory / 'manifest.json')
    projection = manifest.get('driver_projection')
    rows = projection['ledger'] if projection else read(directory / 'ledger.json', [])
    yields = projection['yields'] if projection else [json.loads(s) for s in
              (directory / 'yield.jsonl').read_text().splitlines() if s.strip()]
    manifest['plan_chars'] = len((directory / 'plan.md').read_text())
    normalized = dict(asdict(policy), slug=manifest['slug'], tier=manifest['tier'], tags=manifest['tags'])
    normalized['max_rounds'] += manifest.get('extra_rounds', 0)
    status = stop.evaluate(rows, manifest, yields, normalized)
    if 'review-loop-capped' in status.line:
        manifest['capped'] = True
    save(directory, manifest, rows, yields)
    return policy, manifest, rows, yields, status


def record_report(directory, manifest, text):
    report = parse_report(text)
    target = directory / 'r0'
    target.mkdir(exist_ok=True)
    atomic_text(target / 'report.md', text)
    claims = {c.id: dict(asdict(c), original_state=c.state, sha=manifest['text_sha']) for c in report.claims}
    manifest.update(report={'sha': manifest['text_sha'], 'ok': report.ok, 'claims': list(claims)}, claims=claims)
    ledger.atomic_json(target / 'claims.json', claims)
    return report.ok


def drive(args, directory, policy, manifest, rows, yields, status, spawn):
    ledger.require_frozen(directory)
    if manifest.get('capped') or status.state in {'BLOCKED', 'WAIVED', 'CLEAN'}:
        raise ValueError('review is not OPEN')
    if any(r['severity'] in {'blocker', 'major'} and r['disposition'] == 'open' for r in rows):
        raise ValueError('dispose blocker and major findings before another round')
    kind = 'r0' if args.verb == 'r0' else args.kind
    if not kind:
        done = {s['kind'] for s in yields if s['status'] == 'completed'}
        kind = status.next
        if kind not in {'r0', 'diff'}:
            kind = next((k for k in ('discovery', 'stacked', 'scope-cut') if k not in done), 'confirm')
    if getattr(args, 'report', None):
        if not record_report(directory, manifest, Path(args.report).read_text()):
            save(directory, manifest, rows, yields)
            raise ValueError('invalid fact-check report')
        return
    state = seats.State(**manifest.get('selection', {}), tier=manifest['tier'], slug=manifest['slug'])
    try:
        selected = seats.pick(kind, state, policy)
    except seats.SelectionRefused as error:
        manifest['ladder_exhausted' if 'ladder-exhausted' in str(error) else 'stalled'] = True
        return
    if kind == 'discovery':
        manifest.update(ledger.start_discovery(directory))
    number = max((s['round'] for s in yields), default=0) + 1
    target = directory / ('r0' if kind == 'r0' else f'r{number}')
    target.mkdir(exist_ok=True)
    files = {name: (directory / 'frozen' / manifest['text_sha'] / name).read_text() for name in manifest['files']}
    diff = ''
    previous = manifest['history'][-1].get('from_sha')
    if kind == 'diff':
        before = directory / 'frozen' / previous
        names = set(files) | {p.relative_to(before).as_posix() for p in before.rglob('*') if p.is_file()}
        for name in sorted(names):
            text = files.get(name, '')
            old = directory / 'frozen' / previous / name
            diff += ''.join(difflib.unified_diff(old.read_text().splitlines(True) if old.exists() else [], text.splitlines(True), fromfile=name, tofile=name))
    lens = getattr(args, 'lens', None) or state.lens()
    bs = bundle.BundleState(files, ledger=rows, report=(directory / 'r0/report.md').read_text() if (directory / 'r0/report.md').exists() else '',
                            diff=diff, lens=lens, round=number, material=policy.material,
                            words_path=os.environ.get('PLAN_LOOP_SITE_WORDS') or policy.site_words_file or None,
                            budget_fraction=policy.budget_fraction, chars_per_token=policy.chars_per_token)
    excerpts = directory / 'evidence/excerpts.toml'
    if excerpts.exists():
        bs.excerpts = tomllib.loads(excerpts.read_text())
    failed = 0
    productive = False
    remaining = policy
    def build(leg):
        config = next(item for item in policy.leg if item.id == leg.id)
        built = bundle.build(kind, bundle.Leg(leg.max_request_tokens, config.training), bs)
        ledger.atomic_json(target / f'{leg.id}.scrub.json', built.scrub_record)
        return built

    parallel = None
    if len(selected) > 1:
        # Build and launch the blind first seats before ingesting either reply.
        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            parallel = [pool.submit(spawning.run_seat, kind, replace(state, tier='standard'),
                                    replace(policy, leg=tuple(l for l in policy.leg if l.id == leg.id),
                                            max_failed_attempts=1), target, build,
                                    seat_id=f'initial-{index}', spawn=spawn)
                        for index, leg in enumerate(selected)]
            parallel = [future.result() for future in parallel]
        failed = sum(result['failed_attempts'] for result in parallel)
        excluded = {spawning._leg_family(row['leg']) for result in parallel for row in result['seats']
                    if row.get('attempt_used') or row['status'] == 'completed'}
        remaining = replace(policy, leg=tuple(l for l in policy.leg
                            if spawning._leg_family(l.id) not in excluded))
    for index in range(len(selected)):
        started = time.monotonic()
        def build(leg):
            config = next(item for item in policy.leg if item.id == leg.id)
            bundle_leg = bundle.Leg(leg.max_request_tokens, config.training)
            built = bundle.build(kind, bundle_leg, bs)
            if built.skipped_reason == 'over_budget' and getattr(args, 'per_file', False):
                parts = bundle.build_per_file(kind, bundle_leg, bs)
                # The current manifest has one plan file; never silently drop parts.
                if len(parts) == 1:
                    built = parts[0]
            ledger.atomic_json(target / f'{leg.id}.scrub.json', built.scrub_record)
            return built
        if parallel is not None:
            result = parallel[index]
            if result['status'] != 'completed' and failed < policy.max_failed_attempts:
                retry = spawning.run_seat(kind, replace(state, tier='standard'),
                    replace(remaining, max_failed_attempts=policy.max_failed_attempts - failed),
                    target, build, spawn=spawn, seat_id=f'retry-{index}')
                failed += retry['failed_attempts']
                result = dict(retry, seats=result['seats'] + retry['seats'])
        else:
            result = spawning.run_seat(kind, replace(state, tier='standard'), replace(remaining, max_failed_attempts=policy.max_failed_attempts - failed), target, build, spawn=spawn, seat_id=f's{index}')
            failed += result['failed_attempts']
        for seat in result['seats']:
            seat_id = f's{len(yields) + 1}'
            parsed = {k: seat.get(k) for k in ('status', 'findings')}
            classes = ledger.ingest(rows, parsed, round=number, seat_id=seat_id, family=seat['family'], lens=lens, sha=manifest['text_sha'])
            counts = {severity: sum(f['severity'] == severity for f in seat.get('findings', [])) for severity in ('blocker', 'major', 'minor')}
            row = {k: v for k, v in seat.items() if k not in {'report', 'reply', 'findings'}}
            row.update(round=number, kind=kind, lens=lens, sha=manifest['text_sha'], from_sha=previous if kind == 'diff' else None,
                       counts=counts, new_classes=classes, seconds=time.monotonic() - started)
            yields.append(row)
            if seat['status'] == 'completed':
                state.use(seat['family'])
                productive |= bool(counts['blocker'] + counts['major'])
                state.lens_yields[lens] = state.lens_yields.get(lens, 0) + sum(counts.values())
                if kind == 'r0':
                    record_report(directory, manifest, seat['reply'])
                remaining = replace(remaining, leg=tuple(l for l in remaining.leg if spawning._leg_family(l.id) != spawning._leg_family(seat['leg'])))
        save(directory, manifest, rows, yields)
        if result['status'] != 'completed':
            manifest['stalled'] = True
            break
    state.end_round(productive, counted=kind in {'discovery', 'stacked', 'scope-cut'} or kind == 'confirm' and any(s['kind'] == 'confirm' and s['round'] != number for s in yields))
    manifest['selection'] = {k: v for k, v in asdict(state).items() if k not in {'tier', 'slug'}}
    ledger.close_accepted(rows, manifest, yields)


def parser():
    root = argparse.ArgumentParser()
    verbs = root.add_subparsers(dest='verb', required=True)
    for name in ('init', 'freeze', 'r0', 'round', 'dispose', 'dispose-claim', 'add-review', 'escalate', 'extend', 'waive', 'status', 'check-batch', 'report'):
        p = verbs.add_parser(name)
        if name == 'report':
            p.add_argument('--root', required=True)
            p.add_argument('--changelog', required=True)
            continue
        p.add_argument('--dir', required=True)
        if name == 'init':
            p.add_argument('--slug', required=True)
            p.add_argument('--tier', choices=['standard', 'high'], default='standard')
            p.add_argument('--tag', action='append', default=[])
        if name == 'freeze':
            p.add_argument('--minor', action='store_true')
            p.add_argument('--fixes', default='')
            p.add_argument('--growth-cite')
        if name == 'r0':
            p.add_argument('--report')
        if name == 'round':
            p.add_argument('--kind', choices=['r0', 'discovery', 'stacked', 'scope-cut', 'confirm', 'diff'])
            p.add_argument('--lens')
            p.add_argument('--per-file', action='store_true')
        if name in {'dispose', 'dispose-claim'}:
            p.add_argument('id')
            p.add_argument('disposition')
            for flag in ('sha-fixed', 'disposition-evidence', 'trigger', 'dup-of', 'third-party', 'fact-check', 'evidence'):
                p.add_argument('--' + flag, default='')
        if name == 'add-review':
            for flag in ('family', 'sha', 'file', 'source'):
                p.add_argument('--' + flag, required=True)
        if name in {'add-review', 'extend', 'waive'}:
            p.add_argument('--decision', required=name != 'add-review')
        if name == 'extend':
            p.add_argument('--rounds', type=int, required=True)
        if name == 'escalate':
            p.add_argument('--finding', required=True)
        if name == 'check-batch':
            p.add_argument('batch')
            p.add_argument('--waived-ok', action='store_true')
    return root


def main(argv=None, *, spawn=subprocess.run):
    args = parser().parse_args(argv)
    try:
        if args.verb == 'report':
            code, lines = reporting.report(args.root, args.changelog)
            print('\n'.join(lines))
            return code
        directory = ledger.validate_directory(args.dir)
        if args.verb == 'init':
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.slug):
                raise ValueError('invalid slug')
            directory.mkdir(parents=True, exist_ok=True)
            if any(directory.iterdir()):
                raise ValueError('init requires an empty directory')
            (directory / 'evidence').mkdir()
            atomic_text(directory / 'plan.md', '')
            atomic_text(directory / 'plan-loop.toml', Path(__file__).with_name('plan-loop.default.toml').read_text())
            manifest = ledger.freeze(directory)
            manifest.update(slug=args.slug, tier=args.tier, tags=args.tag)
            save(directory, manifest, [], [])
            print(f'OPEN plan:{args.slug} initialized')
            return 0
        policy, manifest, rows, yields, status = load(directory)
        if args.verb == 'check-batch':
            code, lines = reporting.check_batch(directory, args.batch, waived_ok=args.waived_ok)
            print('\n'.join(lines))
            return code
        if getattr(args, 'decision', None) and not re.fullmatch(r'D-\d+', args.decision):
            raise ValueError('invalid decision id')
        if args.verb == 'freeze':
            manifest.update(ledger.freeze(directory, minor=args.minor, fixes=args.fixes.split(',') if args.fixes else [], rows=rows,
                                         minor_max_chars=policy.minor_max_chars, max_file_chars=policy.max_file_chars, growth_cite=args.growth_cite))
        elif args.verb in {'round', 'r0'}:
            drive(args, directory, policy, manifest, rows, yields, status, spawn)
        elif args.verb == 'extend':
            if args.rounds <= 0 or not manifest.get('capped'):
                raise ValueError('extend requires a cap and positive rounds')
            manifest.update(capped=False, extra_rounds=manifest.get('extra_rounds', 0) + args.rounds)
            manifest.setdefault('extensions', []).append({'decision': args.decision, 'rounds': args.rounds})
        elif args.verb == 'waive':
            if manifest.get('capped') or not manifest.get('stalled') or set(manifest['tags']) & {'auth', 'access', 'secrets'}:
                raise ValueError('waiver refused')
            manifest['waived'] = args.decision
        elif args.verb == 'dispose':
            ledger.dispose(rows, args.id, args.disposition, manifest, **{k: getattr(args, k) for k in
                           ('sha_fixed', 'disposition_evidence', 'trigger', 'dup_of', 'third_party', 'fact_check')},
                           planner_family=policy.planner_family, seats=yields, tier=manifest['tier'], claims=manifest.get('claims'))
        elif args.verb == 'dispose-claim':
            claim = manifest['claims'][args.id]
            if args.disposition not in {'verified', 'false', 'not-load-bearing', 'open-question'}:
                raise ValueError('invalid claim disposition')
            if args.disposition in {'verified', 'not-load-bearing'} and not args.evidence:
                raise ValueError('claim evidence required')
            history = [h['sha'] for h in manifest['history']]
            if args.disposition == 'false' and (args.sha_fixed not in history or history.index(args.sha_fixed) <= history.index(claim['sha'])):
                raise ValueError('claim fix needs a later freeze')
            claim.update(state=args.disposition, evidence=args.evidence, sha_fixed=args.sha_fixed)
            ledger.atomic_json(directory / 'r0/claims.json', manifest['claims'])
        elif args.verb == 'escalate':
            if not any(r['id'] == args.finding and r['severity'] == 'blocker' for r in rows):
                raise ValueError('escalation needs a blocker')
            selection = manifest.setdefault('selection', {})
            selection['level'] = selection.get('level', 1) + 1
        elif args.verb == 'add-review':
            ledger.require_frozen(directory)
            if args.sha != manifest['text_sha'] or args.source != 'operator-chat':
                raise ValueError('review must name current frozen material and operator-chat')
            parsed = ledger.parse_reply(Path(args.file).read_text())
            if parsed['status'] != 'completed':
                raise ValueError('invalid review')
            number = max((s['round'] for s in yields), default=0) + 1
            classes = ledger.ingest(rows, parsed, round=number, seat_id=f'chat{len(yields)}', family=args.family, lens='', sha=args.sha)
            yields.append(dict(round=number, kind='operator-chat', lens='', family=args.family, leg='', run_id=f'chat{len(yields)}', sha=args.sha,
                               witnessed=bool(args.decision), witness_kind='operator-chat', decision=args.decision or '', decision_by='operator',
                               verdict=parsed['verdict'], status='completed', reason='', new_classes=classes, seconds=0,
                               counts={s: sum(f['severity'] == s for f in parsed['findings']) for s in ('blocker', 'major', 'minor')}))
        save(directory, manifest, rows, yields)
        status = load(directory)[-1]
        print(status.line)
        return status.exit_code if args.verb == 'status' else 0
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        print('refused: invalid input or state; material directory must be outside the checkout')
        return 1 if args.verb in {'status', 'check-batch', 'report'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
