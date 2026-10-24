"""Per-host card dispatch and private run records shared by CLI and MCP."""
from __future__ import annotations

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
        path = self._path(record['run_id'])
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(record, indent=2), encoding='utf-8')
        temp.chmod(0o600)
        temp.replace(path)

    def ps(self):
        paths = (self.state / 'runs').glob('run-*/record.json')
        return {'runs': sorted((json.loads(p.read_text(encoding='utf-8')) for p in paths),
                               key=lambda r: r['started'], reverse=True)}

    def route(self, card, role='implement', writer_families=(), review_of=None):
        from plangraph.planner.routing import plan_spawn, SpawnRefused
        families = list(writer_families)
        if review_of:
            record = self.result(review_of)
            family = record.get('writer_family')
            if not family:
                raise SpawnRefused('writer family unavailable')
            families.append(family)
        return plan_spawn(card, role=role, writer_families=families, leg_reader=self.leg_reader)

    async def spawn(self, card=None, prompt='', role='implement', writer_families=(),
                    review_of=None, model=None, leg=None, override_reason=None,
                    dry_run=False, wait_seconds=0, no_fallthrough=False):
        from plangraph.planner.routing import hand_picked, SpawnRefused, SpawnPlan
        from plangraph.planner.legs import DEFAULT_LEGS
        if model or leg or not card:
            chosen = next((l for l in DEFAULT_LEGS if l.id == leg), None) if leg else None
            target = model or (chosen.model if chosen else None)
            if not target:
                raise SpawnRefused('spawn by card; explicit model required for override')
            hand_picked(target, card, override_reason,
                        log_path=self.state / 'spawn-overrides.jsonl', caller='spawner')
            chosen = chosen or replace(DEFAULT_LEGS[0], id='override', model=target,
                                       family=__import__('plangraph.spawner.resolver', fromlist=['detect_model_family']).detect_model_family(target))
            if review_of and chosen.family == self.result(review_of).get('writer_family'):
                raise SpawnRefused('excluded writer family')
            plan = SpawnPlan((chosen,), (), 'override', (), ())
        else:
            plan = self.route(card, role, writer_families, review_of)
        if plan.error or not plan.first_leg:
            raise SpawnRefused(plan.error or 'no eligible leg')
        record = dict(run_id='run-' + uuid.uuid4().hex, card_hash=hashlib.sha256(
            json.dumps(card, sort_keys=True).encode()).hexdigest(), role=role,
            plan=plan.to_dict(), chosen_leg=asdict(plan.first_leg), served_model=None,
            served_family=None, writer_family=plan.first_leg.family, status='running',
            started=time.time(), ended=None, attempts=[], stdout_tail='')
        self._save(record)

        async def execute():
            try:
                for chosen in plan.legs:
                    job = await self.runner.spawn(prompt=prompt, model_or_combo=chosen.model,
                        role=role, dry_run=dry_run, client=chosen.harness)
                    record['job_id'] = job.job_id
                    process = self.runner._processes.get(job.job_id)
                    record['pid'] = process.pid if process else None
                    record['chosen_leg'] = asdict(chosen)
                    self._save(record)
                    task = self.runner._tasks.get(job.job_id)
                    if task:
                        await asyncio.shield(task)
                    stop = self.runner.provider_responses.get(job.job_id)
                    record['attempts'].append(dict(leg=asdict(chosen), status=job.status,
                        skip_reason='provider_stop' if stop else None, provider_response=stop))
                    record.update(status=job.status, served_model=job.served_model,
                        served_family=job.served_family, writer_family=job.served_family or chosen.family,
                        stdout_tail=job.stdout[-16000:])
                    self._save(record)
                    if not stop or no_fallthrough or dry_run:
                        break
            except Exception as exc:
                record.update(status='failed', error=str(exc))
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
        record.update(status='cancelled', ended=time.time())
        self._save(record)
        return record
