"""Per-host card dispatch and private run records shared by CLI and MCP."""
from __future__ import annotations
from .env_redact import first_reason
from .gateway_witness import witness_order, witness_gate_legs
from .exit_outcome import fallthrough_reason, release_attempts, terminal_reason
from .git_guard import prepare_worktree, writer_launch
from .stderr_capture import clean_attempts, write_stderr_log
from plangraph.routing_errors import ParameterRefused
from dataclasses import asdict, replace
from pathlib import Path
import asyncio, hashlib, json, os, socket, time, uuid
from .runner import SpawnerRunner
from .cancel_recorded import cancel_recorded
from .seat_spawn import SeatSpawnMixin
from .private_io import SAVE_READ_ATTEMPTS, read_text_retry
from .output_keep import OUTPUT_KEEP, keep_output
from .item_key import annotate_item
class SpawnService(SeatSpawnMixin):
    def __init__(self, runner=None, state=None, leg_reader=None):
        self.runner = runner or SpawnerRunner()
        self.state = Path(state or os.environ.get('PLANGRAPH_SPAWN_STATE',
            Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'plangraph'))
        from plangraph.routing_support.leg_state_flag import initialize
        from .hung_capture import HungBoundary
        leg_reader = initialize(self, leg_reader, HungBoundary)
        if leg_reader is not None and hasattr(self.runner, '__dict__') and not hasattr(self.runner, 'leg_reader'):  # probes share the planner cache
            self.runner.leg_reader = leg_reader
        self.tasks, self._running_records, self._stderr_captures = {}, {}, {}   # run tasks; job id -> record (Q13/M8)
    def _path(self, run_id):
        if not isinstance(run_id, str) or not run_id.startswith('run-') or not run_id[4:].isalnum():
            raise ValueError('unknown run id')
        return self.state / 'runs' / run_id / 'record.json'
    def result(self, run_id, **read_retry):
        """Read one run record; retry options forward to read_text_retry."""
        try:
            return json.loads(read_text_retry(self._path(run_id), **read_retry))
        except FileNotFoundError:
            raise ValueError('unknown run id') from None

    def _save(self, record):
        from .private_io import atomic_write_private
        from .runner import redact
        from .seat_artifact import publish
        from .env_redact import IDENTIFIER_KEYS, redact_env_values
        # Host identity of the writing machine (C1): only PLANGRAPH_HOST may
        # override it; setdefault keeps a re-saved record's original host.
        record.setdefault('host', os.environ.get('PLANGRAPH_HOST') or socket.gethostname())
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
                        down_reason='not_served_on_host' if (candidate.get('reason') or '').split(':', 1)[0].strip() == 'not_served_on_host' else None,
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
        write_stderr_log(self._stderr_captures.setdefault(record['run_id'], {}), getattr(self.runner, 'jobs', {}), record, self._path(record['run_id']).with_name('stderr.log'), clean)
        host, item_key = record.get('host'), record.get('item_key')
        if record.get('ended') is not None:
            terminal_reason(record, getattr(self.runner, 'jobs', {}).get(record.get('job_id')))
        record.update(clean(record))
        if 'item_key' in record: record['item_key'] = item_key  # validated spawn identity, not a credential
        if host is not None:
            record['host'] = host   # top-level run host is an identifier (D-2960); nested 'host' keys stay redacted
        clean_attempts(record, clean)
        if record.get('status') == 'failed':
            if not record.get('exit_code'):
                record['exit_code'] = 1
            record['error'] = record.get('error') or '; '.join(
                a.get('skip_reason') or 'harness failed' for a in record.get('attempts', [])) or 'harness failed'
        if record.get('error'):
            record['error'] = '; '.join(p[:300] for p in clean(record['error']).split('; '))[:2000]
        if 'stderr_tail' in record:
            record['stderr_tail'] = clean(record['stderr_tail']).encode()[-4096:].decode('utf-8', errors='ignore')
        # A seat's verdict is its stdout: keep it whole (D-3226; the 4 KB tail cut a verdict's head).
        for target in [record, *record.get('attempts', [])]:
            if isinstance(target.get('stdout_tail'), str):
                target['stdout_tail'] = keep_output(clean(target['stdout_tail']))
        atomic_write_private(self._path(record['run_id']), json.dumps(dict(redact(record), **({'item_key': item_key} if 'item_key' in record else {})), indent=2))
        publish(self._path(record['run_id']).parent, record, getattr(self.runner, 'jobs', {}).get(record.get('job_id')))
        if record.get('ended'):
            getattr(self.runner, '_child_envs', {}).pop(record.get('job_id'), None)

    def ps(self):
        from .stall_watchdog import read_runs
        return {'runs': sorted(read_runs(self.state), key=lambda r: r.get('started', 0), reverse=True)}

    def route(self, card, role='implement', writer_families=(), review_of=None, *, benchmark=None, require_bench=False):
        try:
            plan = self._route(card, role, writer_families, review_of,
                               benchmark=benchmark, require_bench=require_bench)
            from plangraph.planner.routing import SpawnRefused, explicit_only_skips
            if plan.error:
                raise SpawnRefused(plan.error)
            return replace(plan, skipped=type(plan.skipped)((*plan.skipped, *explicit_only_skips(role))))
        except Exception as exc:
            self.save_refusal(exc, role)
            raise
    def _route(self, card, role='implement', writer_families=(), review_of=None, *, benchmark=None, require_bench=False):
        from plangraph.routing_support.leg_state_flag import service_plan, SpawnRefused
        families = list(writer_families)
        if review_of:
            record = self.result(review_of, attempts=SAVE_READ_ATTEMPTS)
            family = record.get('given_writer_families') or [record.get('writer_family')]
            if not any(family):
                raise SpawnRefused('writer family unavailable')
            families.extend(family)
        if role == 'review' and not any(str(f or '').strip() for f in families):
            # No writer named = no family excluded (D-1533): fail closed.
            raise SpawnRefused('review needs a writer family (writer_families or review_of)')
        from plangraph.planner.measured_down import measured_down
        plan = service_plan(self, card=card, role=role, writer_families=families, benchmark=benchmark, strict=require_bench)
        from plangraph.planner.legs import leg_active
        live = lambda legs: tuple(leg for leg in legs if leg_active(leg))
        return replace(plan, legs=witness_order(card, role, live(plan.legs), measured_down(self.state)), review_seats=live(plan.review_seats))
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
                     continuation=1, role='implement', cwd=None if record.get('run_worktree') else record['cwd'],
                     chosen_leg=record['chosen_leg'], started=time.time(), ended=None,
                     status='running', exit_code=None, gaps=gaps)
        record['continuation_run_id'] = child['run_id']
        annotate_item(child, {'item': record.get('item_key')}, inherit=True)
        self._save(record)
        self._save(child)
        try:
            chosen = child['chosen_leg']
            worktree = await prepare_worktree(self, child, {'lane_branch': (record.get('run_worktree') or {}).get('branch')}, bool(record.get('run_worktree')), continuation=True)
            job = await self.runner.spawn(prompt=render_fixer_brief([{'text': g} for g in gaps]),
                model_or_combo=chosen['model'], client=chosen['harness'], role='implement', cwd=worktree.get('cwd', child['cwd']), wake_variant='fixer',  # REMOVE-AT-R10
                prompt_record=(self._path(child['run_id']).with_name('prompt.txt'), child, self._save))  # REMOVE-AT-R10
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

    def save_refusal(self, exc, role='implement', *, card=None, placement=None, **extra):
        """Persist pre-service refusals using the same record contract (``extra``: seat fields)."""
        from plangraph.planner.routing import SpawnRefused, BenchmarkError, BenchRequired
        record = dict(run_id='run-' + uuid.uuid4().hex, role=role,
                      started=time.time(), ended=time.time(),
                      status='refused' if isinstance(exc, SpawnRefused) else 'failed',
                      exit_code=3 if isinstance(exc, BenchmarkError) else 2 if isinstance(exc, SpawnRefused) else 1,
                      refusal=type(exc).__name__, error=str(exc),
                      legs=list(exc.legs) if isinstance(exc, BenchRequired) else [],
                      evidence=list(exc.gaps) if isinstance(exc, BenchRequired) else [], **extra)
        if placement is not None:
            record.update(placement=placement, host=placement['host'])
        try:
            annotate_item(record, card)
            self._save(record)
        except Exception as storage_error:
            import logging
            logging.getLogger(__name__).warning('refusal_save_failed: %s', type(storage_error).__name__)
            exc.add_note('Refusal record could not be persisted: ' + type(storage_error).__name__)
        return record

    async def _spawn(self, card=None, prompt='', role='implement', writer_families=(),
                    review_of=None, model=None, leg=None, override_reason=None,
                    dry_run=False, wait_seconds=0, no_fallthrough=False, benchmark=None, require_bench=False, review_scope=None,
                    seats=False, _plan=None, run_worktree=False, _placement=None):
        from plangraph.settings import admit_review_scope
        admit_review_scope(card, role, review_scope)
        from plangraph.planner.routing import hand_picked, SpawnRefused, SpawnPlan
        from plangraph.planner.legs import spawnable_legs
        if not card:
            raise ParameterRefused('spawn by card; card required')
        if seats:
            return await self._spawn_seats(card=card, prompt=prompt, role=role,
                writer_families=writer_families, review_of=review_of, model=model, leg=leg,
                dry_run=dry_run, wait_seconds=wait_seconds, no_fallthrough=no_fallthrough,
                benchmark=benchmark, require_bench=require_bench, review_scope=review_scope,
                override_reason=override_reason, run_worktree=run_worktree, _placement=_placement)
        if _plan is not None:
            plan = _plan
        elif model is not None or leg is not None or not card:
            # Overrides change the selected model, not the card safety contract.
            from plangraph.planner.routing import plan_spawn
            from plangraph.planner.seats import seat_family, writer_parts
            reviewed = ((source.get('given_writer_families') or [f for f in [source.get('writer_family')] if f]) if (source := self.result(review_of, attempts=SAVE_READ_ATTEMPTS)) else []) if review_of else []
            plan_spawn(card, role=role, legs=(), writer_families=[*writer_families, *reviewed])
            chosen = next((l for l in spawnable_legs() if l.id == leg), None) if leg else None
            if leg and chosen is None: raise SpawnRefused('unknown leg')
            if model and chosen and model != chosen.model:
                raise SpawnRefused('model and leg disagree')
            target = model if model is not None else (chosen.model if chosen else None)
            if not target:
                raise SpawnRefused('spawn by card; explicit model required for override')
            from .target import resolve_target
            target, resolved_leg = resolve_target(target, chosen=chosen)
            chosen = resolved_leg or chosen
            # Every refusal runs before hand_picked: a refused override leaves no audit line.
            writers = writer_parts(writer_families) if role == 'review' else set()
            if role == 'review' and not writers and not reviewed:
                raise SpawnRefused('review needs a writer family (writer_families or review_of)')
            from plangraph.planner.legs import NO_VERDICT_SEATS, EXPLICIT_WRITERS, LEAD_WRITER
            from plangraph.routing_support.liveness import require_available
            if role == 'review' and target in NO_VERDICT_SEATS:
                raise SpawnRefused('no-verdict seat model (D-3225): ' + target)
            chosen = chosen or next((l for l in spawnable_legs() if l.model == target), None)
            if chosen is None:
                from .resolver import get_catalog_models, detect_model_family
                from plangraph.planner.legs import Leg
                from plangraph.planner.legs import HARNESSES, TOOL_MODES
                row = next((r for r in get_catalog_models() if r['id'] == target), None)
                # Harness is a catalog column (SPAWNER-REGISTRY-ROUTE), never inferred.
                missing = ('catalog row' if row is None else
                           'tier' if row.get('tier') not in ('trial', 'free', 'subscription', 'paid') else
                           'harness' if row.get('harness') not in HARNESSES else
                           'context_tokens' if type(row.get('context_tokens')) is not int
                           or row['context_tokens'] <= 0 else
                           'tool_mode' if row.get('tool_mode', 'tools') not in TOOL_MODES else None)
                if missing:
                    raise SpawnRefused('override model missing registry data: ' + missing)
                context = row.get('context_tokens', 0)
                from plangraph.planner.routing import WINDOW_TOKENS
                windows = tuple(w for w, n in WINDOW_TOKENS.items() if n <= context)
                chosen = Leg('override', row['tier'], row['harness'], target,
                             detect_model_family(target), windows, context,
                             bool(row.get('trains_on_prompts', True)), roles=(role,),
                             tool_mode=row.get('tool_mode', 'tools'))
            if reviewed and {seat_family(f) for f in reviewed} <= {seat_family(chosen.family)}:
                raise SpawnRefused('excluded writer family')
            # D-3084 per-part: a seat is excluded only when it covers no writer part.
            if writers and writers <= {seat_family(chosen.family)}:
                raise SpawnRefused('excluded writer family')
            require_available(chosen, self.leg_reader, card)
            if role == 'implement' and chosen.id != LEAD_WRITER and chosen.id not in EXPLICIT_WRITERS:
                raise SpawnRefused('writer disabled (D-3828): ' + chosen.id)
            if chosen.tier == 'paid':
                available = self._route(card, role, writer_families, review_of,
                    benchmark=benchmark, require_bench=require_bench)
                from plangraph.planner.measured_down import measured_down
                down = measured_down(self.state)
                free = [candidate.id for candidate in available.legs if candidate.tier == 'free' and candidate.id not in down]
                if override_reason is None and not free:
                    override_reason = 'No eligible free leg is up on this host'
                if not isinstance(override_reason, str) or len(override_reason.strip()) < 10:
                    raise SpawnRefused('paid pin refused; free legs up: ' + ', '.join(free) +
                        '; --override-reason required (D-4382: no free leg witnesses is a valid reason)')
                override_reason = override_reason.strip()
            hand_picked(target, card, override_reason,
                        log_path=self.state / 'spawn-overrides.jsonl', caller='spawner')
            plan = SpawnPlan((chosen,), (), 'override', (), ())
        else:
            # The spawn wrapper owns refusal persistence; route() owns it only
            # for standalone route requests, otherwise one error is saved twice.
            plan = self._route(card, role, writer_families, review_of, benchmark=benchmark, require_bench=require_bench)
        if plan.error or not plan.first_leg:
            raise SpawnRefused(plan.error or 'no eligible leg')
        plan = replace(plan, legs=witness_gate_legs(card, role, plan.legs), placement=_placement or plan.placement)  # before run artifacts
        record = dict(run_id='run-' + uuid.uuid4().hex, card_hash=hashlib.sha256(
            json.dumps(card, sort_keys=True).encode()).hexdigest(), role=role,
            plan=plan.to_dict(), chosen_leg=asdict(plan.first_leg) if dry_run or (role == 'review' and (model is not None or leg is not None)) else None, served_model=None,
            served_family=None, writer_family=plan.first_leg.family, status='running', given_writer_families=list(writer_families) or ((source := self.result(review_of, attempts=SAVE_READ_ATTEMPTS)).get('given_writer_families') or [f for f in [source.get('writer_family')] if f] if review_of else []),
             started=time.time(), ended=None, attempts=[], stdout_tail='', exit_code=None, override_reason=override_reason,
            cwd=getattr(self.runner, 'default_cwd', os.getcwd()), **({'placement': _placement} if _placement else {}))
        pack = await asyncio.to_thread(self._annotate_spawn, record, prompt, card, record['cwd'], plan, model is not None or leg is not None); annotate_item(record, card); self._save(record)  # D-3622, D-3649: one pack per spawn, off the loop

        async def execute():
            try:
                from .run_watchdog import prepare_run, terminal_record
                limits, live_log = prepare_run(self, record, card)
                launched = False
                for chosen in plan.legs:
                    from .inline_diff import InlineDiffError
                    try:
                        leg_prompt = await self._prepare_leg_prompt(prompt, card, role, chosen, pack, document_review=leg is not None or dry_run,
                            deadline=record['started'] + limits['max_s'] if role == 'review' else None)   # Dry runs never launch; live document reviews require --leg.
                    except InlineDiffError as exc:
                        record['attempts'].append(dict(leg=asdict(chosen), status='skipped',
                            skip_reason='inline_diff_failed: ' + str(exc),
                            served_model='unwitnessed', served_family='unwitnessed',
                            served_witness_reason='leg skipped before launch'))
                        continue
                    launched = True
                    leg_prompt, writer_env = writer_launch(leg_prompt, role, chosen.model, record['run_id'])
                    worktree = await prepare_worktree(self, record, card, run_worktree and not dry_run)
                    job = await self._launch_leg(record, chosen, prompt=leg_prompt, model_or_combo=chosen.model,
                        role=role, dry_run=dry_run, client=chosen.harness, watchdog=limits, live_log=live_log, extra_env=writer_env, **worktree,
                        prompt_record=(self._path(record['run_id']).with_name('prompt.txt'), record, self._save))  # REMOVE-AT-R10
                    stop = self.runner.provider_responses.get(job.job_id)
                    if terminal_record(record, job):
                        break
                    unavailable = fallthrough_reason(self, chosen, job, record)
                    if unavailable:
                        job.status = 'failed'
                        job.exit_code = job.exit_code or 1
                    reason = unavailable or ('provider_stop' if stop else first_reason(job.stderr) or f'harness exit code {job.exit_code}'
                        if job.status in ('failed', 'skipped') else None)
                    stop_status = stop.get('status') if isinstance(stop, dict) else None
                    down_token = (unavailable or '').split(':', 1)[0].strip()
                    record['attempts'].append(dict(leg=asdict(chosen), status=job.status,
                        # Only failed/skipped attempts count; only 402/429 override unavailable.
                        down_reason=('credit_limit' if stop_status == 402 else 'usage_limit' if stop_status == 429 else
                            down_token if down_token in ('not_served_on_host', 'credit_limit', 'usage_limit') else None)
                            if job.status in ('failed', 'skipped') else None,
                        served_model=job.served_model, served_family=job.served_family,
                        served_witness_reason=job.served_witness_reason,
                        skip_reason=reason, provider_response=stop,
                        stdout_tail=job.stdout, stderr_tail=job.stderr))
                    if job.status in ('completed', 'dry_run') and not stop and not unavailable:
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
                if not launched:
                    record.update(status='failed', exit_code=1, error='; '.join(
                        a['skip_reason'] for a in record['attempts']) or 'no leg launched')
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
                terminal_reason(record)
                release_attempts(self, record)
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
        return self.result(record['run_id'], attempts=SAVE_READ_ATTEMPTS)

    async def cancel(self, run_id):
        return await cancel_recorded(self, run_id)
