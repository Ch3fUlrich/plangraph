"""Seat runs for :class:`~plangraph.spawner.service.SpawnService` (split from service.py, D-3198).

``spawn(seats=True)`` routes once and starts one review run per seat ``route`` picked;
an inline-only seat leg gets the card's diff inlined in its prompt.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import json
import os
import time
import uuid
from pathlib import Path

from .seat_artifact import reserve_seat_families, seat_reasons, sync_seat, sync_seat_group


def resources(service):
    from .overflow import available_memory_mb
    return 0, available_memory_mb()


def admit(service, options, *, card=None, preview=False):
    """Enforce RAM admission even without an overflow configuration."""
    if options.get('dry_run'):
        return None
    from . import overflow
    path = Path(os.environ.get('PLANGRAPH_OVERFLOW_HOSTS',
                              '~/.config/plangraph/overflow-hosts.json')).expanduser()
    try:
        config = json.loads(path.read_text())
        if not isinstance(config, dict):
            config = {}
    except (OSError, ValueError):
        config = {}
    role = options.get('role', 'implement')
    registry = overflow.registry_caps()
    local, remote_first = overflow.placement_policy(registry, config)
    if preview and not remote_first:
        return None
    role_mb, floor = overflow.local_limits(registry, role)
    _, memory = resources(service)
    if not remote_first and not overflow.memory_refusal(memory, role_mb, floor):
        return None
    repo = Path(options.get('cwd') or (card or {}).get('cwd') or
                ((card or {}).get('diff_ref') or {}).get('repo') or
                getattr(service.runner, 'default_cwd', None) or os.getcwd()).resolve()
    public = overflow.repos_public(config, repo, (card or {}).get('diff_ref'))
    benchmark_local = remote_first and options.get('benchmark') is not None
    if benchmark_local:
        public = False  # A local-only database makes every remote ineligible.
    rows = overflow.ready_records(config, role)
    if remote_first:
        rows = {name: row for name, row in rows.items()
                if name != local and overflow.host_config(registry, name).get('id') != local}
    health = overflow.local_health_refusal(config, memory) if remote_first else None
    flags = []
    for key in ('role', 'model', 'leg', 'override_reason', 'review_of', 'review_scope'):
        if options.get(key) is not None:
            flags.extend(('--' + key.replace('_', '-'), str(options[key])))
    for family in options.get('writer_families', ()):
        flags.extend(('--writer-family', family))
    for key in ('seats', 'no_fallthrough', 'require_bench', 'run_worktree'):
        if options.get(key):
            flags.append('--' + key.replace('_', '-'))
    while True:
        decision = overflow.decide(memory, role_mb, floor, rows, public,
                                   remote_first=remote_first, local_refusal=health,
                                   host_order=tuple(config.get('hosts', {})))
        if benchmark_local and decision.reason == 'local_fallback_no_remote':
            decision = overflow.Decision(None, 'local_benchmark_database')
        if options.get('benchmark') is not None and decision.host is not None:
            decision = overflow.Decision(None, 'local benchmark database cannot be forwarded')
        placement = dict(host=decision.host or local, reason=decision.reason,
                         free_mb=rows[decision.host]['mem_available_mb'] if decision.host else memory)
        if preview:
            return placement
        if remote_first:
            options['_placement'] = placement
            if decision.host is None and decision.reason in ('local_fallback_no_remote', 'local_benchmark_database'):
                return None
        if decision.host is None:
            record = {'run_id': 'run-' + uuid.uuid4().hex,
                      'status': 'refused' if remote_first else 'queued',
                      'reason': decision.reason, 'overflow_reason': decision.reason,
                      'exit_code': 2 if remote_first else 5}
            break
        name = decision.host
        try:
            if options.get('cwd') is not None or 'cwd' in (card or {}):
                raise overflow.RemoteRefusal('explicit cwd cannot overflow to another checkout')
            result = overflow.remote_spawn(name, config['hosts'][name], card or {},
                                           options.get('prompt', ''), flags)
            refusal = 'remote spawn failed'
        except overflow.RemoteRefusal as exc:
            result, refusal = None, str(exc)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result, refusal = None, overflow.failure_reason(exc)
        if result is not None:
            record = dict(result, host=name, overflow_reason=decision.reason, overflow_remote=True)
            record.setdefault('status', 'unknown')
            break
        rows[name]['refusal'] = refusal
    if remote_first:
        record.update(host=placement['host'], placement=placement)
    return record


# These children expose skill tools, but no repository/shell tools (D-4681).
INLINE_DIFF_HARNESSES = frozenset({'opencode'})


def _needs_inline_diff(leg, card):
    return (getattr(leg, 'tool_mode', 'tools') == 'inline-only'
            or (getattr(leg, 'harness', None) in INLINE_DIFF_HARNESSES
                and card.get('diff_ref') is not None))


def annotate_review_warning(record):
    from plangraph.planner.legs import REVIEW_UNFIT_SEATS
    model = (record.get('chosen_leg') or {}).get('model')
    if (record.get('role') == 'review' and (record.get('plan') or {}).get('p_source') == 'override'
            and model in REVIEW_UNFIT_SEATS):
        warning = 'review_unfit: ' + model
        if warning not in record.setdefault('warnings', []):
            record['warnings'].append(warning)


def _refuse_oversized_diff(exc, leg):
    from .context_pack import PromptBoundError
    if exc.chars is not None:
        raise PromptBoundError(
            f'prompt_too_large: inline_diff_oversized: chars {exc.chars} estimated_tokens {(exc.chars + 2) // 3} '
            f'max_request_tokens {leg.max_request_tokens}') from None


class SeatSpawnMixin:

    _seat_reasons = staticmethod(seat_reasons)
    _sync_seat = staticmethod(sync_seat)

    def admit_spawn(self, options, *, card=None):
        """Check admission and persist any queued or remote run through the service."""
        record = admit(self, options, card=card)
        if record is not None:
            self._save(record)
        return record

    async def spawn(self, *args, **kwargs):
        options = {}
        try:
            import inspect
            bound = inspect.signature(self._spawn).bind(*args, **kwargs)
            options = dict(bound.arguments)
            card = options.get('card')
            from plangraph.retired_models import check_retired
            check_retired(options)
            check_retired(card)
            from .prompt_input import resolve_cwd
            cwd = await asyncio.to_thread(resolve_cwd, options.get('cwd'), card)
            if cwd is not None:
                options['cwd'] = bound.arguments['cwd'] = cwd
            from .prompt_guard import admit_prompt
            from plangraph.planner.routing import SpawnRefused
            if options.get('role', 'implement') == 'implement':
                admit_prompt(options.get('prompt'), SpawnRefused)
            admitted = await asyncio.to_thread(self.admit_spawn, options, card=card)
            if admitted is not None:
                return admitted
            if card and card.get('findings') and options.get('role', 'implement') == 'implement':
                from .fixer import split_findings, render_fixer_brief
                from plangraph.settings import admit_fixer_threshold
                # No keeper item is held here: the trusted layers are global
                # and the card's project/phase layers may only tighten (R3).
                groups = split_findings(card['findings'], limit=admit_fixer_threshold(card=card))
                briefs = [admit_prompt(render_fixer_brief(g), SpawnRefused) for g in groups]
                if len(groups) > 1:
                    async def dispatch(group, brief):
                        child = dict(card, findings=list(group))
                        child_options = dict(options, card=child, prompt=brief)
                        return await self._spawn(**child_options)
                    return {'writers': await asyncio.gather(*(dispatch(g, b) for g, b in zip(groups, briefs)))}
                options['prompt'] = briefs[0]
                return await self._spawn(**options)
            dispatch_kwargs = bound.kwargs
            if '_placement' in options:
                dispatch_kwargs['_placement'] = options['_placement']
            return await self._spawn(*bound.args, **dispatch_kwargs)
        except Exception as exc:
            self.save_refusal(exc, options.get('role', kwargs.get('role', args[2] if len(args) > 2 else 'implement')),
                              card=options.get('card', kwargs.get('card', args[0] if args else None)),
                              **({'placement': options['_placement']} if options.get('_placement') else {}))
            raise

    def _annotate_spawn(self, record, prompt, card, cwd, plan, explicit):
        """Surface explicit-leg advisories before the first save or launch."""
        from .context_pack import annotate
        pack = annotate(record, prompt, card, cwd)
        if record.get('chosen_leg'):
            annotate_review_warning(record)
        return pack

    async def _prepare_leg_prompt(self, prompt, card, role, leg, pack=None,
                                  document_review=False, deadline=None):
        """Keep Git/context packaging off the RPC loop and inside the review budget."""
        from .inline_diff import InlineDiffError
        remaining = None if deadline is None else deadline - time.time()
        if remaining is not None and remaining <= 0:
            raise InlineDiffError('review prompt packaging deadline exceeded')
        work = asyncio.to_thread(self._leg_prompt, prompt, card, role, leg, pack,
                                 document_review=document_review,
                                 diff_timeout=60 if remaining is None else min(60, remaining))
        try:
            return await asyncio.wait_for(work, timeout=remaining)
        except asyncio.TimeoutError:
            raise InlineDiffError('review prompt packaging deadline exceeded') from None

    async def _launch_leg(self, record, chosen, *, routed=False, **options):
        """One normal launch, at most one capped review retry; never recurse or reroute."""
        if record.get('cwd') is not None:
            options.setdefault('cwd', record['cwd'])
        async def launch(prompt_record, **kwargs):
            job = await self.runner.spawn(prompt_record=prompt_record, **kwargs)
            self._running_records[job.job_id] = record
            process = self.runner._processes.get(job.job_id)
            owned = getattr(self.runner, '_owned', {}).get(job.job_id, {})
            record.update(job_id=job.job_id, pid=process.pid if process else None,
                cwd=getattr(job, 'cwd', None) or getattr(self.runner, 'default_cwd', os.getcwd()))
            if routed:
                record.update(writer_family=chosen.family,
                    **{k: bool(v) if k == 'job_object' else v for k, v in owned.items() if k != 'pid'})
            else:
                record.update(pid_start=owned.get('pid_start'),
                    **{k: v for k, v in owned.items() if k in ('job_name', 'escapees')})
            self._save(record)
            task = self.runner._tasks.get(job.job_id)
            if task:
                if not routed:
                    await asyncio.shield(task)  # preserve base dispatch's inner/outer cancellation contract
                else:
                    await asyncio.wait([task])  # monitor cancellation ends the leg, not dispatch
                    if task.cancelled():
                        from .cancel_job import publish_cancel
                        publish_cancel(job)
                    elif task.exception() is not None:
                        raise task.exception()
            if kwargs.get('reasoning_cap') and self._path(record['run_id']).with_name('cancelled').exists():
                job.status = 'cancelled'
            return job

        def protected(job):
            return (job.status not in ('completed', 'failed')
                or record.get('status') in ('cancelled', 'timeout', 'timed_out', 'stalled')
                or getattr(job, 'survivors', ()) or record.get('reason') in
                ('cancelled', 'timeout', 'timed_out', 'stalled', 'kill_incomplete', 'git_config_mutated', 'writer_no_changes')
                or any(e.get('event') in ('watchdog_timeout', 'watchdog_stalled') for e in job.events))

        def exhausted(job):
            if record.get('role') != 'review' or protected(job):
                return None
            return next((e for e in getattr(job, 'events', ())
                         if e.get('event') == 'opencode_reasoning_exhausted'), None)

        def record_attempt(job):
            record['attempts'].append(dict(leg=asdict(chosen), job_id=job.job_id,
                status='failed', skip_reason='reasoning_output_limit',
                served_model=job.served_model, served_family=job.served_family,
                served_witness_reason=job.served_witness_reason,
                **(dict(request_side_model=job.request_side_model, witness_kind=job.witness_kind) if routed else {}),
                stdout_tail=job.stdout, stderr_tail=job.stderr, events=job.events))
            record.setdefault('events', []).extend(job.events)

        job = await launch(**options)
        evidence = exhausted(job)
        if evidence is None:
            return job
        if self._path(record['run_id']).with_name('cancelled').exists():
            job.status = 'cancelled'
            return job
        limits = options.get('watchdog') or {}
        cap, deadline = limits.get('max_s', 1500), limits.get('deadline')
        if deadline is None and cap is not None: deadline = record['started'] + cap
        remaining = deadline - time.time() if deadline is not None else float('inf')
        if evidence.get('cap_available') and not record.get('reasoning_retry') and remaining > 30:
            record_attempt(job)
            record['events'].append(dict(event='reasoning_capped_retry', leg=chosen.id,
                                         thinking='disabled'))
            record['reasoning_retry'] = 1  # durable before launching the sole retry
            self._save(record)
            job = await launch(**dict(options, reasoning_cap=True,
                watchdog=dict(limits, max_s=remaining if deadline is not None else None, hard_max=True)))
            if protected(job):
                return job
            cap_unavailable = any(e.get('event') == 'render_refused' and
                str(e.get('reason', '')).startswith('reasoning_output_limit:') for e in job.events)
            if exhausted(job) is None and not cap_unavailable:
                return job
        job.status, job.exit_code = 'failed', job.exit_code or 1
        job.events.append(dict(event='reasoning_output_limit'))
        record_attempt(job)
        if routed:
            record.update(request_side_model=job.request_side_model, witness_kind=job.witness_kind,
                served_model_mismatch=any(e.get('event') == 'served_model_mismatch' for e in job.events))
        record.update(status='failed', reason='reasoning_output_limit', exit_code=job.exit_code,
            served_model=job.served_model, served_family=job.served_family,
            served_witness_reason=job.served_witness_reason,
            writer_family=chosen.family if job.served_family in (None, '', 'unwitnessed') else job.served_family,
            stdout_tail=record.get('stdout_tail', '') + job.stdout,
            stderr_tail=record.get('stderr_tail', '') + job.stderr)
        self._save(record)  # terminal evidence must survive even if the caller never saves again
        return job

    def _leg_prompt(self, prompt, card, role, leg, pack=None, document_review=False, diff_timeout=60):
        """Assemble one leg's payload, then enforce its request limit."""
        from .target import quota_paused
        from plangraph.routing_errors import SpawnRefused
        if quota_paused(getattr(self, 'state', None) or getattr(self, '_state', None), role, leg):
            raise SpawnRefused('quota pause')
        from . import context_pack
        prompt = self._unbounded_leg_prompt(prompt, card, role, leg, pack, document_review, diff_timeout)
        return context_pack.bound_prompt(prompt, leg.max_request_tokens)

    def _unbounded_leg_prompt(self, prompt, card, role, leg, pack=None, document_review=False, diff_timeout=60):
        """The prompt one leg gets: an inline-only leg gets the card's context pack (D-3622), a review leg its diff.

        ``pack`` is the spawn's one ``context_pack.annotate`` result (D-3649); without it the pack is built here.
        ``document_review`` allows explicit ``--leg`` document reviews and dry-run planning; live routed
        inline-only review legs need the card's diff and refuse without ``diff_ref`` (r3).
        OpenCode tools-mode reviews inline a diff when supplied, but also support non-diff reviews."""
        if getattr(leg, 'tool_mode', 'tools') == 'inline-only':
            from . import context_pack
            prompt = context_pack.pack_prompt(prompt, card, getattr(self.runner, 'default_cwd', None) or os.getcwd(), pack)
        if role == 'review' and _needs_inline_diff(leg, card):
            from .inline_diff import InlineDiffError, inline_diff_prompt
            try:
                prompt = inline_diff_prompt(prompt, card.get('diff_ref'), max_chars=leg.max_request_tokens * 3,
                                            allow_document=document_review, timeout=diff_timeout)
            except InlineDiffError as exc:
                _refuse_oversized_diff(exc, leg)
                raise
        return prompt

    async def _spawn_seats(self, *, card, prompt, role, writer_families, review_of, model, leg,
                           **options):
        """One review run per seat ``route`` picked; callers never name legs (D-3198).

        Reserve entire canonical families, not individual spare indices, so
        alternate legs of one family can never belong to different slots.
        """
        from plangraph.planner.routing import SpawnRefused
        if role != 'review' or model is not None or leg is not None:
            raise SpawnRefused('seats needs role review without model or leg')
        plan = self._route(card, role, writer_families, review_of,
                           benchmark=options.pop('benchmark'), require_bench=options.pop('require_bench'))
        if plan.error:
            raise SpawnRefused(plan.error)
        from plangraph.planner.seats import seat_family
        writers = list(writer_families)
        if review_of:
            source = self.result(review_of)
            writers.extend(source.get('given_writer_families') or [source.get('writer_family')])
        spares, owners = reserve_seat_families(plan, writers)
        group = 'seats-' + uuid.uuid4().hex
        coverage = {c['leg']: c for c in plan.seat_coverage}
        # Each seat record carries only its own coverage entry plus the group id.
        own = [dict(coverage.get(seat.id, {'leg': seat.id}), seat_group=group,
                    family=seat.family, keeper_family=seat_family(seat.family),
                    eligible_families=[f for f, owner in owners.items() if owner == i])
               for i, seat in enumerate(plan.review_seats)]
        runs = await asyncio.gather(*(
            self._spawn(card=card, prompt=prompt, role=role, writer_families=writer_families,
                        review_of=review_of, _plan=replace(
                             plan, legs=(seat, *spares[i]), review_seats=(seat,), seat_coverage=(own[i],)),
                        **options)
            for i, seat in enumerate(plan.review_seats)), return_exceptions=True)
        # A seat that raised still gets a record (with its group and coverage); the
        # others keep running and are awaited by callers.
        runs = [self.save_refusal(r, role, card=card, seat_leg=seat.id, plan={'seat_coverage': [own[i]]})
                if isinstance(r, Exception) else r
                for i, (seat, r) in enumerate(zip(plan.review_seats, runs))]
        records = [r for r in runs if not isinstance(r, BaseException)]
        sync_seat_group(records, self._save)
        resolved = [r['plan']['seat_coverage'][0] for r in records]
        families = [c.get('family') for c in resolved]
        reasons = self._seat_reasons(families, [c['reason'] for c in resolved if c.get('reason')])
        envelope = {'seat_group': group, 'seats': records, 'seat_coverage': resolved, 'reasons': list(reasons)}
        self._save_seat_group(envelope)   # before any re-raise: launched seats keep their group
        for r in runs:
            if isinstance(r, BaseException):
                raise r
        return envelope

    def _save_seat_group(self, envelope):
        """Persist the seat set next to the run records (``<state>/seat-groups/<id>.json``)."""
        from .private_io import atomic_write_private
        atomic_write_private(self.state / 'seat-groups' / (envelope['seat_group'] + '.json'),
                             json.dumps({'seat_group': envelope['seat_group'],
                                         'runs': [r.get('run_id') for r in envelope['seats']],
                                         'seat_coverage': envelope['seat_coverage']}, sort_keys=True))
