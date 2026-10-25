"""Per-host card dispatch and private run records shared by CLI and MCP."""
from __future__ import annotations
from plangraph.routing_errors import ParameterRefused

import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .runner import SpawnerRunner


class SpawnService:
    def __init__(self, runner=None, state=None, leg_reader=None):
        self.runner = runner or SpawnerRunner()
        self.state = Path(state or os.environ.get('PLANGRAPH_SPAWN_STATE',
            Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'plangraph'))
        self.leg_reader = leg_reader
        self.tasks = {}

    def _path(self, run_id):
        if not isinstance(run_id, str) or not run_id.startswith('run-') or not run_id[4:].isalnum():
            raise ValueError('unknown run id')
        return self.state / 'runs' / run_id / 'record.json'

    def result(self, run_id):
        try:
            return json.loads(self._path(run_id).read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise ValueError('unknown run id') from None

    def _save(self, record):
        from .private_io import atomic_write_private
        from .runner import redact
        from .env_redact import IDENTIFIER_KEYS, redact_env_values
        record.setdefault('served_model', 'unwitnessed')
        record.setdefault('served_family', 'unwitnessed')
        for target in [record, *record.get('attempts', [])]:
            if not target.get('served_model') or not target.get('served_family'):
                target['served_model'] = target.get('served_model') or 'unwitnessed'
                target['served_family'] = target.get('served_family') or 'unwitnessed'
            if target['served_model'] == 'unwitnessed':
                target.setdefault('served_witness_reason', 'no authenticated harness telemetry; skipped or unobserved leg')
        if record.get('ended') is not None:
            attempted = {a['leg'].get('id') if isinstance(a.get('leg'), dict) else a.get('leg')
                         for a in record.get('attempts', [])}
            plan = record.get('plan') or {}
            for candidate in plan.get('skipped', []):
                leg = candidate.get('leg')
                if leg not in attempted:
                    record.setdefault('attempts', []).append(dict(leg=leg, status='skipped',
                        skip_reason=candidate.get('skip_reason') or candidate.get('reason'),
                        served_model='unwitnessed', served_family='unwitnessed',
                        served_witness_reason='leg skipped before launch'))
                    attempted.add(leg)
            for leg in plan.get('legs', []):
                if leg['id'] not in attempted:
                    record.setdefault('attempts', []).append(dict(leg=leg, status='skipped',
                        skip_reason='earlier_leg_succeeded' if record.get('status') in ('completed', 'dry_run') else 'run_terminated_before_leg',
                        served_model='unwitnessed', served_family='unwitnessed',
                        served_witness_reason='leg not launched'))
        env = dict(os.environ)
        env.update(getattr(self.runner, '_child_envs', {}).get(record.get('job_id'), {}))
        def clean(value):
            if isinstance(value, str):
                return redact(redact_env_values(value, env))
            if isinstance(value, (list, tuple, set)):
                return [clean(v) for v in value]
            if isinstance(value, dict):
                return {k: v if k in IDENTIFIER_KEYS else clean(v) for k, v in value.items()}
            return value
        record.update(clean(record))
        for attempt in record.get('attempts', []):
            if attempt.get('skip_reason'):
                attempt['skip_reason'] = clean(attempt['skip_reason'])[:300]
        if record.get('status') == 'failed':
            if not record.get('exit_code'):
                record['exit_code'] = 1
            record['error'] = record.get('error') or '; '.join(
                a.get('skip_reason') or 'harness failed' for a in record.get('attempts', [])) or 'harness failed'
        if record.get('error'):
            record['error'] = '; '.join(p[:300] for p in clean(record['error']).split('; '))[:2000]
        for stream in ('stdout_tail', 'stderr_tail'):
            if stream in record:
                record[stream] = clean(record[stream]).encode()[-4096:].decode('utf-8', errors='ignore')
        atomic_write_private(self._path(record['run_id']), json.dumps(redact(record), indent=2))
        if record.get('ended'):
            getattr(self.runner, '_child_envs', {}).pop(record.get('job_id'), None)

    def ps(self):
        from .stall_watchdog import read_runs
        return {'runs': sorted(read_runs(self.state), key=lambda r: r.get('started', 0), reverse=True)}

    def route(self, card, role='implement', writer_families=(), review_of=None, *, benchmark=None, require_bench=False):
        try:
            plan = self._route(card, role, writer_families, review_of,
                               benchmark=benchmark, require_bench=require_bench)
            if plan.error:
                from plangraph.planner.routing import SpawnRefused
                raise SpawnRefused(plan.error)
            return plan
        except Exception as exc:
            self.save_refusal(exc, role)
            raise

    def _route(self, card, role='implement', writer_families=(), review_of=None, *, benchmark=None, require_bench=False):
        from plangraph.planner.routing import plan_spawn, SpawnRefused
        families = list(writer_families)
        if review_of:
            record = self.result(review_of)
            family = record.get('writer_family')
            if not family:
                raise SpawnRefused('writer family unavailable')
            families.append(family)
        return plan_spawn(card, role=role, writer_families=families, leg_reader=self.leg_reader, benchmark=benchmark, strict=require_bench)

    async def spawn(self, *args, **kwargs):
        try:
            import inspect
            bound = inspect.signature(self._spawn).bind(*args, **kwargs)
            options = dict(bound.arguments)
            card = options.get('card')
            if card and card.get('findings') and options.get('role', 'implement') == 'implement':
                from .fixer import split_findings, render_fixer_brief
                from plangraph.settings import admit_fixer_threshold
                groups = split_findings(card['findings'], limit=admit_fixer_threshold(card))
                if len(groups) > 1:
                    async def dispatch(group):
                        child = dict(card, findings=list(group))
                        child_options = dict(options, card=child, prompt=render_fixer_brief(group))
                        return await self._spawn(**child_options)
                    return {'writers': await asyncio.gather(*(dispatch(g) for g in groups))}
                options['prompt'] = render_fixer_brief(groups[0])
                return await self._spawn(**options)
            return await self._spawn(*args, **kwargs)
        except Exception as exc:
            self.save_refusal(exc, kwargs.get('role', args[2] if len(args) > 2 else 'implement'))
            raise

    async def _continue_writer(self, record, output):
        from .fixer import remaining_gaps, render_fixer_brief
        gaps = remaining_gaps(output)
        reason = ('nonzero_exit' if record.get('exit_code') != 0 or record.get('status', 'completed') != 'completed' else
                  'continuation_limit' if record.get('continuation') else
                  'not_writer' if record.get('role') != 'implement' else
                  'clean_report' if not gaps else 'remaining_gaps')
        record['continuation_reason'] = reason
        if reason != 'remaining_gaps' or record.get('continuation_run_id'):
            return
        child = dict(run_id='run-' + uuid.uuid4().hex, parent_run_id=record['run_id'],
                     continuation=1, role='implement', cwd=record['cwd'],
                     chosen_leg=record['chosen_leg'], started=time.time(), ended=None,
                     status='running', exit_code=None, gaps=gaps)
        record['continuation_run_id'] = child['run_id']
        self._save(record)
        self._save(child)
        try:
            chosen = child['chosen_leg']
            job = await self.runner.spawn(prompt=render_fixer_brief([{'text': g} for g in gaps]),
                model_or_combo=chosen['model'], client=chosen['harness'], role='implement', cwd=child['cwd'])
            child['job_id'] = job.job_id
            self._save(child)
            task = self.runner._tasks.get(job.job_id)
            if task:
                await asyncio.shield(task)
            child.update(status=job.status, exit_code=job.exit_code, stdout_tail=job.stdout)
            await self._continue_writer(child, job.stdout)
        except Exception as exc:
            child.update(status='failed', exit_code=1, error=str(exc), continuation_reason='spawn_failed')
        finally:
            child['ended'] = time.time()
            self._save(child)

    def save_refusal(self, exc, role='implement'):
        """Persist pre-service refusals using the same record contract."""
        from plangraph.planner.routing import SpawnRefused, BenchmarkError, BenchRequired
        record = dict(run_id='run-' + uuid.uuid4().hex, role=role,
                      started=time.time(), ended=time.time(),
                      status='refused' if isinstance(exc, SpawnRefused) else 'failed',
                      exit_code=3 if isinstance(exc, BenchmarkError) else 2 if isinstance(exc, SpawnRefused) else 1,
                      refusal=type(exc).__name__, error=str(exc),
                      legs=list(exc.legs) if isinstance(exc, BenchRequired) else [],
                      evidence=list(exc.gaps) if isinstance(exc, BenchRequired) else [])
        try:
            self._save(record)
        except Exception as storage_error:
            import logging
            logging.getLogger(__name__).warning('refusal_save_failed: %s', type(storage_error).__name__)
            exc.add_note('Refusal record could not be persisted: ' + type(storage_error).__name__)
        return record

    async def _spawn(self, card=None, prompt='', role='implement', writer_families=(),
                    review_of=None, model=None, leg=None, override_reason=None,
                    dry_run=False, wait_seconds=0, no_fallthrough=False, benchmark=None, require_bench=False, review_scope=None):
        from plangraph.settings import admit_review_scope
        admit_review_scope(card, role, review_scope)
        from plangraph.planner.routing import hand_picked, SpawnRefused, SpawnPlan
        from plangraph.planner.legs import DEFAULT_LEGS
        if not card:
            raise ParameterRefused('spawn by card; card required')
        if model is not None or leg is not None or not card:
            # Overrides change the selected model, not the card safety contract.
            from plangraph.planner.routing import plan_spawn
            plan_spawn(card, role=role, legs=())
            chosen = next((l for l in DEFAULT_LEGS if l.id == leg), None) if leg else None
            if leg and chosen is None:
                raise SpawnRefused('unknown leg')
            if model and chosen and model != chosen.model:
                raise SpawnRefused('model and leg disagree')
            target = model if model is not None else (chosen.model if chosen else None)
            if not target:
                raise SpawnRefused('spawn by card; explicit model required for override')
            hand_picked(target, card, override_reason,
                        log_path=self.state / 'spawn-overrides.jsonl', caller='spawner')
            chosen = chosen or next((l for l in DEFAULT_LEGS if l.model == target), None)
            if chosen is None:
                from .resolver import get_catalog_models, detect_model_family
                from plangraph.planner.legs import Leg
                row = next((r for r in get_catalog_models() if r['id'] == target), None)
                # Harness registration must be explicit, independently of transport.
                harness = row.get('harness') if row else None
                if (row is None or row.get('tier') not in ('trial', 'free', 'subscription', 'paid')
                        or harness not in ('opencode', 'qodercli', 'agy')
                        or type(row.get('context_tokens')) is not int or row['context_tokens'] <= 0):
                    raise SpawnRefused('override model missing registry data')
                context = row.get('context_tokens', 0)
                from plangraph.planner.routing import WINDOW_TOKENS
                windows = tuple(w for w, n in WINDOW_TOKENS.items() if n <= context)
                chosen = Leg('override', row['tier'], harness, target,
                             detect_model_family(target), windows, context,
                             bool(row.get('trains_on_prompts', True)), roles=(role,))
            if review_of and chosen.family == self.result(review_of).get('writer_family'):
                raise SpawnRefused('excluded writer family')
            if role == 'review' and chosen.family in writer_families:
                raise SpawnRefused('excluded writer family')
            plan = SpawnPlan((chosen,), (), 'override', (), ())
        else:
            # The spawn wrapper owns refusal persistence; route() owns it only
            # for standalone route requests, otherwise one error is saved twice.
            plan = self._route(card, role, writer_families, review_of, benchmark=benchmark, require_bench=require_bench)
        if plan.error or not plan.first_leg:
            raise SpawnRefused(plan.error or 'no eligible leg')
        record = dict(run_id='run-' + uuid.uuid4().hex, card_hash=hashlib.sha256(
            json.dumps(card, sort_keys=True).encode()).hexdigest(), role=role,
            plan=plan.to_dict(), chosen_leg=asdict(plan.first_leg) if dry_run else None, served_model=None,
            served_family=None, writer_family=plan.first_leg.family, status='running',
            started=time.time(), ended=None, attempts=[], stdout_tail='', exit_code=None,
            cwd=getattr(self.runner, 'default_cwd', os.getcwd()))
        self._save(record)

        async def execute():
            try:
                for chosen in plan.legs:
                    job = await self.runner.spawn(prompt=prompt, model_or_combo=chosen.model,
                        role=role, dry_run=dry_run, client=chosen.harness)
                    record['job_id'] = job.job_id
                    record['cwd'] = getattr(job, 'cwd', None) or getattr(self.runner, 'default_cwd', os.getcwd())
                    process = self.runner._processes.get(job.job_id)
                    record['pid'] = process.pid if process else None
                    self._save(record)
                    task = self.runner._tasks.get(job.job_id)
                    if task:
                        await asyncio.shield(task)
                    stop = self.runner.provider_responses.get(job.job_id)
                    from .env_redact import unavailable_reason
                    unavailable = unavailable_reason(job.exit_code, job.stdout, job.stderr)
                    if unavailable:
                        job.status = 'failed'
                        job.exit_code = job.exit_code or 1
                    reason = unavailable or ('provider_stop' if stop else
                        (job.stderr.strip().splitlines()[0] if job.stderr.strip() else f'harness exit code {job.exit_code}')
                        if job.status in ('failed', 'skipped') else None)
                    record['attempts'].append(dict(leg=asdict(chosen), status=job.status,
                        served_model=job.served_model, served_family=job.served_family,
                        served_witness_reason=job.served_witness_reason,
                        skip_reason=reason, provider_response=stop,
                        stdout_tail=job.stdout, stderr_tail=job.stderr))
                    if job.status in ('completed', 'dry_run') and not unavailable and not stop:
                        record['chosen_leg'] = asdict(chosen)
                    record.update(status=job.status, exit_code=job.exit_code if job.exit_code is not None else (0 if job.status == 'dry_run' else 1), served_model=job.served_model,
                        served_family=job.served_family, served_witness_reason=job.served_witness_reason,
                        writer_family=chosen.family if job.served_family in (None, '', 'unwitnessed') else job.served_family,
                        stdout_tail=record.get('stdout_tail', '') + job.stdout,
                        stderr_tail=record.get('stderr_tail', '') + job.stderr, events=record.get('events', []) + getattr(job, 'events', []))
                    self._save(record)
                    if (not stop and not unavailable) or no_fallthrough or dry_run:
                        if not dry_run:
                            await self._continue_writer(record, job.stdout)
                        break
            except Exception as exc:
                from plangraph.planner.routing import BenchRequired, BenchmarkError, SpawnRefused
                if isinstance(exc, SpawnRefused):
                    record.update(status='refused', refusal=type(exc).__name__,
                                  exit_code=3 if isinstance(exc, BenchmarkError) else 2,
                                  evidence=list(exc.gaps) if isinstance(exc, BenchRequired) else [],
                                  legs=list(exc.legs) if isinstance(exc, BenchRequired) else [],
                                  error=str(exc))
                else:
                    record.update(status='failed', exit_code=1, error=str(exc))
            finally:
                record['ended'] = time.time()
                self._save(record)

        task = asyncio.create_task(execute())
        self.tasks[record['run_id']] = task
        if dry_run or wait_seconds is None:
            await task
        elif wait_seconds > 0:
            try:
                await asyncio.wait_for(asyncio.shield(task), wait_seconds)
            except asyncio.TimeoutError:
                pass
        return self.result(record['run_id'])

    async def cancel(self, run_id):
        record = self.result(run_id)
        if record.get('job_id') in self.runner.jobs:
            await self.runner.cancel(record['job_id'])
        elif record['status'] == 'running' and record.get('pid'):
            await self.runner.tree_killer(record['pid'])
        task = self.tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        record.update(status='cancelled', exit_code=1, ended=time.time())
        self._save(record)
        return record
