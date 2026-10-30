"""Seat runs for :class:`~plangraph.spawner.service.SpawnService` (split from service.py, D-3198).

``spawn(seats=True)`` routes once and starts one review run per seat ``route`` picked;
an inline-only seat leg gets the card's diff inlined in its prompt.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import uuid


class SeatSpawnMixin:
    """Needs ``_spawn``, ``_route``, ``save_refusal`` and ``state`` from the service."""

    @staticmethod
    def _leg_prompt(prompt, card, role, leg):
        """The prompt one leg gets: an inline-only review leg gets the card's diff inlined."""
        if role != 'review' or getattr(leg, 'tool_mode', 'tools') != 'inline-only':
            return prompt
        from .inline_diff import inline_diff_prompt
        return inline_diff_prompt(prompt, card.get('diff_ref'), max_chars=leg.max_request_tokens * 3)

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
        seat_families = {s.family for s in plan.review_seats}
        spares = []
        for spare in plan.legs:
            if spare.family not in seat_families:
                spares.append(spare)
                seat_families.add(spare.family)   # one spare per family
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
        runs = [self.save_refusal(r, role, seat_leg=seat.id, plan={'seat_coverage': [own[i]]})
                if isinstance(r, Exception) else r
                for i, (seat, r) in enumerate(zip(plan.review_seats, runs))]
        for r in runs:
            if isinstance(r, BaseException):
                raise r
        envelope = {'seat_group': group, 'seats': runs, 'seat_coverage': list(plan.seat_coverage)}
        self._save_seat_group(envelope)
        return envelope

    def _save_seat_group(self, envelope):
        """Persist the seat set next to the run records (``<state>/seat-groups/<id>.json``)."""
        from .private_io import atomic_write_private
        atomic_write_private(self.state / 'seat-groups' / (envelope['seat_group'] + '.json'),
                             json.dumps({'seat_group': envelope['seat_group'],
                                         'runs': [r.get('run_id') for r in envelope['seats']],
                                         'seat_coverage': envelope['seat_coverage']}, sort_keys=True))
