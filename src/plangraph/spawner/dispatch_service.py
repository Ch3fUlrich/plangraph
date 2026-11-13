"""Dispatch service extracted from the job runner."""
from . import runner

def spawn_service(*args, **kwargs):
    """Shared hardened dispatch for the public CLI and MCP entry points."""
    from .service import SpawnService
    from dataclasses import asdict
    from pathlib import Path

    class DispatchService(SpawnService):

        async def _continue_writer(self, record, output):
            if record.get('status') != 'needs_input':
                return await super()._continue_writer(record, output)

        def __init__(self, *args, completion_sink=None, keeper_sink=None, **kwargs):
            super().__init__(*args, **kwargs)
            from .notify import FileInbox
            self.completion_sink = completion_sink or FileInbox(self.state)
            from .notify import KeeperSink
            self.keeper_sink = keeper_sink or KeeperSink()
            self._running_records = {}
            if hasattr(self.runner, '__dict__'):
                self.runner.state = self.state
                self.runner.on_sample = lambda jid: self._save(self._running_records[jid]) if jid in self._running_records else None

        async def cancel(self, run_id):
            self._path(run_id).parent.exists() and self._path(run_id).with_name('cancelled').touch()
            return await super().cancel(run_id)

        def _save(self, record, *, retry=False):
            import socket
            record.setdefault('host', runner.os.environ.get('PLANGRAPH_HOST_NAME') or socket.gethostname())
            from .notify import completion_event, CommandSink, KeeperNotConfigured, KeeperSink
            job = getattr(self.runner, 'jobs', {}).get(record.get('job_id'))
            if job is not None:
                record['last_activity_ts'] = getattr(job, 'last_activity_ts', None)
                for stream in ('stdout', 'stderr'):
                    record[stream + '_tail'] = getattr(job, stream, '')
                for attempt in record.get('attempts', []):
                    if attempt.get('status') in ('failed', 'skipped') and (not attempt.get('skip_reason')):
                        attempt['skip_reason'] = runner.redact(runner.failure_reason(job))
            terminal = record.get('ended') is not None and record.get('status') != 'running'
            record.setdefault('wait_cmd', 'plangraph-spawn wait ' + record['run_id'])
            if not retry:
                super()._save(record)
            changed = False
            if terminal:
                if 'completion' not in record:
                    record['completion'] = completion_event(record, record.get('cwd') or getattr(self.runner, 'default_cwd', runner.os.getcwd()))
                    if record.get('status') == 'needs_input':
                        record['completion'].update(event='NEEDS_INPUT', question=record['question'])
                    changed = True
            if changed:
                super()._save(record)
            if terminal:
                from .notify import deliver_verdict
                deliver_verdict(self._path(record['run_id']).parent, record, self.keeper_sink)
                for recipient, sink in (('parent', self.completion_sink), ('keeper', self.keeper_sink)):
                    marker = self._path(record['run_id']).parent / (recipient + '.delivered')
                    if marker.exists():
                        continue
                    try:
                        no_keeper_item = recipient == 'keeper' and isinstance(sink, KeeperSink) and sink.url and sink.token_file and (not record['completion'].get('item'))
                        if not no_keeper_item:
                            sink.deliver(recipient, record['completion'])
                        from .private_io import atomic_write_private
                        atomic_write_private(marker, 'delivered\n')
                    except KeeperNotConfigured:
                        now = runner.time.monotonic()
                        last = runner._keeper_unconfigured_last_warned
                        if last is None or now - last >= runner._KEEPER_UNCONFIGURED_WARN_S:
                            runner._keeper_unconfigured_last_warned = now
                            runner.logging.getLogger(__name__).warning('completion delivery skipped: keeper not configured (queued)')
                    except Exception:
                        runner.logging.getLogger(__name__).exception('completion delivery failed: %s', recipient)
                command = runner.os.environ.get('PLANGRAPH_SPAWN_NOTIFY_CMD')
                if command:
                    for recipient in ('parent', 'keeper'):
                        marker = self._path(record['run_id']).parent / (recipient + '.hook.delivered')
                        if not marker.exists():
                            try:
                                CommandSink(command).deliver(recipient, record['completion'])
                                from .private_io import atomic_write_private
                                atomic_write_private(marker, 'delivered\n')
                            except Exception:
                                runner.logging.getLogger(__name__).exception('completion hook failed')

        def retry_completions(self):
            """Re-drive durable terminal records after a supervisor/bridge outage."""
            for record in self.ps()['runs']:
                recipients = ['parent', 'keeper', 'verdict'] if record.get('role') == 'review' else ['parent', 'keeper']
                markers = [r + '.delivered' for r in recipients]
                if runner.os.environ.get('PLANGRAPH_SPAWN_NOTIFY_CMD'):
                    markers += [r + '.hook.delivered' for r in ('parent', 'keeper')]
                pending = any((not (self._path(record['run_id']).parent / m).exists() for m in markers))
                if record.get('ended') is not None and record.get('status') != 'running' and pending:
                    try:
                        self._save(record, retry=True)
                    except Exception:
                        runner.logging.getLogger(__name__).exception('completion retry failed')

        async def wait(self, run_id, timeout=None):
            if timeout is not None and timeout < 0:
                raise ValueError('timeout must be nonnegative')
            deadline = None if timeout is None else runner.time.monotonic() + timeout
            while True:
                record = self.result(run_id)
                if record.get('ended') is not None and record['status'] != 'running':
                    return record
                from .exit_outcome import orphaned
                gone = orphaned(record, runner.time.time())
                if gone is not None:
                    self._save(gone)
                    return gone
                if deadline is not None and runner.time.monotonic() >= deadline:
                    raise TimeoutError('run wait timed out: ' + run_id)
                await runner.asyncio.sleep(0.02)

        def _route(self, card, role='implement', writer_families=(), review_of=None, **options):
            from . import leg_state
            original = self.leg_reader
            from .leg_probe import DispatchReader
            self.leg_reader = DispatchReader(original, leg_state.leg_status)
            try:
                return super()._route(card, role, writer_families, review_of, **options)
            finally:
                self.leg_reader = original

        async def _spawn(self, card=None, prompt='', role='implement', writer_families=(), review_of=None, model=None, leg=None, override_reason=None, dry_run=False, wait_seconds=0, no_fallthrough=False, benchmark=None, require_bench=False, review_scope=None, seats=False, _plan=None, run_worktree=False, _placement=None, cwd=None, _queued_record=None):
            from .private_io import SAVE_READ_ATTEMPTS
            from .cancel_recorded import register_execute
            from .run_watchdog import leg_limits, terminal_status, thresholds
            from .question_bridge import rounds_exhausted
            if rounds_exhausted(self, card, role):
                from plangraph.routing_errors import SpawnRefused
                raise SpawnRefused('question_rounds_exhausted')
            if seats or model is not None or leg is not None or (not card):
                return await super()._spawn(card=card, prompt=prompt, role=role, writer_families=writer_families, review_of=review_of, model=model, leg=leg, override_reason=override_reason, dry_run=dry_run, wait_seconds=wait_seconds, no_fallthrough=not seats or no_fallthrough, benchmark=benchmark, require_bench=require_bench, review_scope=review_scope, seats=seats, run_worktree=run_worktree, _placement=_placement, cwd=cwd, _queued_record=_queued_record)
            from plangraph.settings import admit_review_scope
            admit_review_scope(card, role, review_scope)

            def restrict(p, tried):
                from .seat_artifact import restrict_seat_plan
                return p if _plan is None else restrict_seat_plan(p, _plan, tried)
            plan = _plan if _plan is not None else self._route(card, role, writer_families, review_of, benchmark=benchmark, require_bench=require_bench)
            from .resolver import plan_for_launch_async
            if hasattr(plan, '__dataclass_fields__'):
                plan = await plan_for_launch_async(runner.replace(plan, placement=_placement or plan.placement), dry_run=dry_run)
            run_id = (_queued_record or {}).get('run_id') or 'run-' + runner.uuid.uuid4().hex
            record = dict(_queued_record or {}, run_id=run_id, card_hash=runner.hashlib.sha256(runner.json.dumps(card, sort_keys=True).encode()).hexdigest(), role=role, plan=plan.to_dict(), chosen_leg=None, served_model=None, served_family=None, request_side_model=None, witness_kind=None, writer_family=None, status='running', started=runner.time.time(), given_writer_families=list(writer_families) or ((source := self.result(review_of, attempts=SAVE_READ_ATTEMPTS)).get('given_writer_families') or [f for f in [source.get('writer_family')] if f] if review_of else []), ended=None, attempts=[], stdout_tail='', stderr_tail='', exit_code=None, cwd=cwd or getattr(self.runner, 'default_cwd', runner.os.getcwd()), **{'placement': _placement} if _placement else {})
            from .context_pack import annotate
            from .item_key import annotate_item
            pack = await runner.asyncio.to_thread(annotate, record, prompt, card, record['cwd'])
            annotate_item(record, card)
            for skipped in plan.to_dict()['skipped']:
                if any((marker in str(skipped) for marker in ('not_served_on_host', 'harness_missing'))):
                    record.setdefault('skipped', []).append(skipped)
                    record['attempts'].append(dict(leg=skipped.get('leg'), status='skipped', skip_reason=skipped.get('skip_reason') or skipped.get('reason')))
            if not _queued_record:
                self._save(record)
            if not plan.legs or plan.error:
                host_skips = [s for s in plan.skipped if any((marker in str(s) for marker in ('not_served_on_host', 'harness_missing')))]
                from plangraph.routing_support.liveness import provider_exhausted
                exhausted = not plan.legs and (not plan.error or provider_exhausted(plan.skipped))
                record.update(status='failed' if host_skips else 'limited' if exhausted else 'refused', exit_code=1 if host_skips else 4 if exhausted else 2, ended=runner.time.time(), error='; '.join((s.get('skip_reason') or s.get('reason', '') for s in host_skips)) if host_skips else plan.error, evidence=plan.to_dict()['skipped'])
                await register_execute(self, record, None, _queued_record)
                return self.result(run_id, attempts=SAVE_READ_ATTEMPTS)

            async def execute():
                tried = set()
                current = plan
                try:
                    limits = thresholds(card, role=role)
                    live_log = str(self._path(run_id).parent / 'live.log')
                    record['live_log'] = live_log
                    for _ in range(len(plan.legs) + 2):
                        if (watchdog := leg_limits(record, limits)) is None:
                            self._save(record)
                            return
                        chosen = next((l for l in current.legs if l.id not in tried), None)
                        if chosen is None or current.error:
                            break
                        tried.add(chosen.id)
                        from .leg_probe import before_launch
                        probe_skip = await before_launch(self.runner, chosen, role, dry_run)
                        if probe_skip:
                            record['attempts'].append(dict(leg=runner.asdict(chosen), status='skipped', skip_reason=probe_skip))
                            self._save(record)
                            if no_fallthrough:
                                break
                            continue
                        from .inline_diff import InlineDiffError
                        try:
                            leg_prompt = await self._prepare_leg_prompt(prompt, card, role, chosen, pack, document_review=leg is not None or dry_run, deadline=watchdog.get('deadline'))
                        except InlineDiffError as exc:
                            record['attempts'].append(dict(leg=runner.asdict(chosen), status='skipped', skip_reason='inline_diff_failed: ' + runner.redact(str(exc))))
                            continue
                        leg_prompt, writer_env = runner.writer_launch(leg_prompt, role, chosen.model, run_id)
                        worktree = await runner.prepare_worktree(self, record, card, run_worktree and (not dry_run))
                        if (watchdog := leg_limits(record, limits)) is None:
                            self._save(record)
                            return
                        job = await self._launch_leg(record, chosen, routed=True, prompt=leg_prompt, model_or_combo=chosen.model, role=role, dry_run=dry_run, client=chosen.harness, watchdog=watchdog, live_log=live_log, extra_env=writer_env, **worktree, prompt_record=(self._path(run_id).with_name('prompt.txt'), record, self._save))
                        if record.get('reason') == 'reasoning_output_limit':
                            return
                        if job.survivors:
                            record['attempts'].append(dict(leg=runner.asdict(chosen), status='failed', skip_reason='kill_incomplete'))
                            record.update(status='failed', exit_code=1, reason='kill_incomplete', survivors=job.survivors, events=record.get('events', []) + job.events)
                            return
                        if self._path(run_id).with_name('cancelled').exists():
                            job.status = 'cancelled'
                        stop = runner.redact(self.runner.provider_responses.get(job.job_id))
                        if job.status in ('stalled', 'timeout', 'cancelled'):
                            record['attempts'].append(dict(leg=runner.asdict(chosen), status=job.status, skip_reason=job.status, served_model=job.served_model or 'unwitnessed', served_family=job.served_family or 'unwitnessed'))
                            record.update(status=terminal_status(job.status, role), exit_code=1 if job.exit_code is None else job.exit_code, reason=job.status, watchdog=next((e for e in job.events if e['event'].startswith('watchdog_')), None), events=record.get('events', []) + job.events)
                            return
                        if runner.refusal_record(record, job):
                            record['attempts'].append(dict(leg=runner.asdict(chosen), status='failed', skip_reason=record['reason'], stdout_tail=job.stdout, stderr_tail=job.stderr, provider_response=stop))
                            return
                        reason = None
                        unavailable = runner.fallthrough_reason(self, chosen, job, record)
                        if unavailable:
                            job.status = 'failed'
                            job.exit_code = job.exit_code or 1
                        if job.status in ('failed', 'skipped'):
                            reason = runner.redact(runner.failure_reason(job, stop))
                        if unavailable:
                            reason = runner.redact(unavailable)
                        record['attempts'].append(dict(leg=runner.asdict(chosen), status=job.status, served_model=job.served_model, served_family=job.served_family, served_witness_reason=job.served_witness_reason, request_side_model=job.request_side_model, witness_kind=job.witness_kind, skip_reason=reason or ('provider_stop' if stop else None), provider_response=stop, stdout_tail=job.stdout, stderr_tail=job.stderr))
                        if stop and (not unavailable) and (job.status != 'cancelled'):
                            runner.notify_leg_refused(self, record, chosen, reason or 'provider_stop')
                        if job.status in ('completed', 'dry_run', 'terminated_after_answer') and (not unavailable) and (not stop):
                            record['chosen_leg'] = runner.asdict(chosen)
                        record.update(status=job.status, exit_code=job.exit_code if job.exit_code is not None else 0 if job.status == 'dry_run' else 1, served_model=job.served_model, served_family=job.served_family, served_witness_reason=job.served_witness_reason, request_side_model=job.request_side_model, witness_kind=job.witness_kind, served_model_mismatch=any((e.get('event') == 'served_model_mismatch' for e in getattr(job, 'events', None) or [])), writer_family=chosen.family if job.served_family in (None, '', 'unwitnessed') else job.served_family, stdout_tail=record.get('stdout_tail', '') + job.stdout, stderr_tail=record.get('stderr_tail', '') + job.stderr, events=record.get('events', []) + getattr(job, 'events', []))
                        if job.status == 'needs_input' or (not stop and (not unavailable)) or no_fallthrough or dry_run:
                            if not dry_run and job.status != 'needs_input':
                                await self._continue_writer(record, job.stdout)
                            return
                        if role == 'review':
                            record.update(status='running', exit_code=None)
                        self._save(record)
                        current = plan if unavailable else self._route(card, role, writer_families, review_of, benchmark=benchmark, require_bench=require_bench)
                        if hasattr(current, '__dataclass_fields__'):
                            current = restrict(await plan_for_launch_async(runner.replace(current, placement=_placement or current.placement), dry_run=dry_run), tried)
                        for skipped in current.to_dict()['skipped']:
                            if any((marker in str(skipped) for marker in ('not_served_on_host', 'harness_missing'))) and skipped not in record.setdefault('skipped', []):
                                record['skipped'].append(skipped)
                                record['attempts'].append(dict(leg=skipped.get('leg'), status='skipped', skip_reason=skipped.get('skip_reason') or skipped.get('reason')))
                        record['plan'] = current.to_dict()
                    else:
                        record.update(status='failed', exit_code=1, error='attempt iteration limit exceeded')
                        return
                    if not any((a.get('status') != 'skipped' for a in record['attempts'])):
                        record.update(status='failed', exit_code=1, error='; '.join((a['skip_reason'] for a in record['attempts'] if a.get('skip_reason'))) or 'no leg launched')
                    elif any((a.get('skip_reason') and any((m in a['skip_reason'].lower() for m in ('model unavailable', 'not served', 'unknown model', 'not_served_on_host', 'harness_missing'))) for a in record['attempts'])):
                        record.update(status='failed', exit_code=1, error='; '.join((a['skip_reason'] for a in record['attempts'] if a.get('skip_reason'))))
                    elif record['attempts'] and all((l.id in tried for l in current.legs)) and current.legs:
                        record.update(status='failed', exit_code=1, error='router repeated exhausted legs')
                    else:
                        record.update(status='limited', exit_code=4, evidence=current.to_dict()['skipped'])
                except Exception as exc:
                    from plangraph.planner.routing import BenchRequired, BenchmarkError, SpawnRefused
                    if isinstance(exc, SpawnRefused):
                        record.update(status='refused', exit_code=3 if isinstance(exc, BenchmarkError) else 2, refusal=type(exc).__name__, error=runner.redact(str(exc)), evidence=list(exc.gaps) if isinstance(exc, BenchRequired) else [], legs=list(exc.legs) if isinstance(exc, BenchRequired) else [])
                        return
                    record.update(status='failed', exit_code=1, error=runner.redact(str(exc)))
                finally:
                    if record['status'] == 'running' and record.get('job_id') in self.runner.jobs and self.runner.jobs[record['job_id']].survivors:
                        record.update(status='failed', exit_code=1, reason='kill_incomplete', survivors=self.runner.jobs[record['job_id']].survivors)
                    elif record['status'] == 'running':
                        record.update(status='cancelled', exit_code=1)
                    record['ended'] = runner.time.time()
                    runner.terminal_reason(record, job if record.get('job_id') else None)
                    runner.release_attempts(self, record)
                    self._save(record)
            if (task := (await register_execute(self, record, execute, _queued_record))) is None:
                return self.result(run_id, attempts=SAVE_READ_ATTEMPTS)
            if dry_run or wait_seconds is None:
                await task
            elif wait_seconds > 0:
                try:
                    await runner.asyncio.wait_for(runner.asyncio.shield(task), wait_seconds)
                except runner.asyncio.TimeoutError:
                    pass
            return self.result(run_id, attempts=SAVE_READ_ATTEMPTS)
    return DispatchService(*args, **kwargs)
