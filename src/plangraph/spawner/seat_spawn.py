"""Seat runs for :class:`~plangraph.spawner.service.SpawnService` (split from service.py, D-3198).

``spawn(seats=True)`` routes once and starts one review run per seat ``route`` picked;
an inline-only seat leg gets the card's diff inlined in its prompt.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
import time
import uuid
from pathlib import Path


def resources(service):
    from .overflow import available_memory_mb
    return 0, available_memory_mb()


def admit(service, options, *, card=None):
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
    role_mb, floor = overflow.local_limits(overflow.registry_caps(), role)
    live, memory = resources(service)
    if not overflow.memory_refusal(memory, role_mb, floor):
        return None
    repo = Path(((card or {}).get('diff_ref') or {}).get('repo') or
                getattr(service.runner, 'default_cwd', None) or os.getcwd()).resolve()
    public = str(repo) in [str(Path(p).expanduser().resolve()) for p in config.get('public_repos', [])]
    rows = overflow.ready_records(config, role)
    flags = []
    for key in ('role', 'model', 'leg', 'override_reason', 'review_of', 'review_scope'):
        if options.get(key) is not None:
            flags.extend(('--' + key.replace('_', '-'), str(options[key])))
    for family in options.get('writer_families', ()):
        flags.extend(('--writer-family', family))
    for key in ('seats', 'no_fallthrough', 'require_bench'):
        if options.get(key):
            flags.append('--' + key.replace('_', '-'))
    while True:
        decision = overflow.decide(live, memory, role_mb, floor, rows, public)
        if options.get('benchmark') is not None and decision.host is not None:
            decision = overflow.Decision(None, 'local benchmark database cannot be forwarded')
        if decision.host is None:
            record = {'run_id': 'run-' + uuid.uuid4().hex, 'status': 'queued',
                      'reason': decision.reason, 'overflow_reason': decision.reason, 'exit_code': 5}
            break
        name = decision.host
        try:
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
    service._save(record)
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
    if os.environ.get('PLANGRAPH_CONTEXT_STRICT') == '1' and exc.chars is not None:
        raise PromptBoundError(
            f'inline_diff_oversized: chars {exc.chars} estimated_tokens {(exc.chars + 2) // 3} '
            f'max_request_tokens {leg.max_request_tokens}') from None


class SeatSpawnMixin:
    """Needs ``_spawn``, ``_route``, ``save_refusal`` and ``state`` from the service."""

    async def spawn(self, *args, **kwargs):
        try:
            import inspect
            bound = inspect.signature(self._spawn).bind(*args, **kwargs)
            options = dict(bound.arguments)
            card = options.get('card')
            from .prompt_guard import admit_prompt
            from plangraph.planner.routing import SpawnRefused
            if options.get('role', 'implement') == 'implement':
                admit_prompt(options.get('prompt'), SpawnRefused)
            admitted = admit(self, options, card=card)
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
            return await self._spawn(*args, **kwargs)
        except Exception as exc:
            self.save_refusal(exc, kwargs.get('role', args[2] if len(args) > 2 else 'implement'),
                              card=kwargs.get('card', args[0] if args else None))
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

    def _leg_prompt(self, prompt, card, role, leg, pack=None, document_review=False, diff_timeout=60):
        """Assemble one leg's payload, then apply opt-in whole-prompt admission."""
        from . import context_pack
        prompt = self._unbounded_leg_prompt(prompt, card, role, leg, pack, document_review, diff_timeout)
        if os.environ.get('PLANGRAPH_CONTEXT_STRICT') == '1':
            return context_pack.bound_prompt(prompt, leg.max_request_tokens)
        return prompt

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

        Seat *i* falls through only to spare legs of a family no other seat holds,
        dealt round-robin so two seats never land on the same spare.
        """
        from plangraph.planner.routing import SpawnRefused
        if role != 'review' or model is not None or leg is not None:
            raise SpawnRefused('seats needs role review without model or leg')
        plan = self._route(card, role, writer_families, review_of,
                           benchmark=options.pop('benchmark'), require_bench=options.pop('require_bench'))
        if plan.error:
            raise SpawnRefused(plan.error)
        from plangraph.planner.seats import seat_family
        # Keeper families: gpt/openai and claude/anthropic are one family each.
        seat_families = {seat_family(s.family) for s in plan.review_seats}
        spares = []
        for spare in plan.legs:
            if seat_family(spare.family) not in seat_families:
                spares.append(spare)
                seat_families.add(seat_family(spare.family))   # one spare per family
        n = len(plan.review_seats)
        group = 'seats-' + uuid.uuid4().hex
        coverage = {c['leg']: c for c in plan.seat_coverage}
        # Each seat record carries only its own coverage entry plus the group id.
        own = [dict(coverage.get(seat.id, {'leg': seat.id}), seat_group=group) for seat in plan.review_seats]
        runs = await asyncio.gather(*(
            self._spawn(card=card, prompt=prompt, role=role, writer_families=writer_families,
                        review_of=review_of, _plan=replace(
                            plan, legs=(seat, *spares[i::n]), seat_coverage=(own[i],)),
                        **options)
            for i, seat in enumerate(plan.review_seats)), return_exceptions=True)
        # A seat that raised still gets a record (with its group and coverage); the
        # others keep running and are awaited by callers.
        runs = [self.save_refusal(r, role, card=card, seat_leg=seat.id, plan={'seat_coverage': [own[i]]})
                if isinstance(r, Exception) else r
                for i, (seat, r) in enumerate(zip(plan.review_seats, runs))]
        records = [r for r in runs if not isinstance(r, BaseException)]
        envelope = {'seat_group': group, 'seats': records, 'seat_coverage': list(plan.seat_coverage)}
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
