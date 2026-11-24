"""The weak pair's two seats for a smart seat that ended without a verdict (rule 3.66 f10 clause 6)."""
import asyncio
from dataclasses import replace

from .seat_artifact import lone_smart_seat, weak_pair


def ended_without_verdict(run):
    """A smart seat run that raised, failed or ended with no resolved leg gave no verdict."""
    return isinstance(run, BaseException) or run.get('status') != 'completed' or not run.get('chosen_leg')


async def weak_pair_after_smart(service, plan, runs, writers, coverage, group, *, card, prompt, role,
                                writer_families, review_of, options):
    """``(runs, coverage rows)`` of both weak seats when the lone smart seat gave no verdict, else ``([], [])``.

    Both seats are launched in the smart seat's group and both are required.
    """
    from plangraph.planner.seats import seat_family, writer_parts
    pair = weak_pair(plan, writer_parts(writers)) if lone_smart_seat(plan) and ended_without_verdict(runs[0]) else ()
    more = [dict(coverage.get(leg.id, {'leg': leg.id}), seat_group=group, family=leg.family,
                 keeper_family=seat_family(leg.family), eligible_families=[seat_family(leg.family)])
            for leg in pair]
    launched = await asyncio.gather(*(
        service._spawn(card=card, prompt=prompt, role=role, writer_families=writer_families,
                       review_of=review_of, _plan=replace(
                           plan, legs=(leg,), review_seats=(leg,), seat_coverage=(more[i],)),
                       **options)
        for i, leg in enumerate(pair)), return_exceptions=True)
    return [service.save_refusal(r, role, card=card, seat_leg=leg.id, plan={'seat_coverage': [more[i]]})
            if isinstance(r, Exception) else r for i, (leg, r) in enumerate(zip(pair, launched))], more
