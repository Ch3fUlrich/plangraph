"""Round cap (D-5813, rule 3.35): no 4th implement spawn per item and PR; replan instead."""
import json

from plangraph.routing_errors import SpawnRefused

MAX_ROUNDS = 3
# Fallback only when the route table cannot select a planning leg (D-5696 order).
FALLBACK_REPLAN_ORDER = ('cx-gpt-6.1-sol', 'agy-gemini-4-hydrogen', 'claude-sonnet-high')


class RoundCapRefused(SpawnRefused):
    reason = 'round_cap'

    def __init__(self, payload):
        super().__init__(json.dumps(dict(reason=self.reason, replan_required=payload), sort_keys=True))
        self.payload = payload


def round_ref(card):
    """PR number or branch the card works on, or None."""
    for key in ('pr', 'pr_number', 'branch'):
        value = card.get(key) if isinstance(card, dict) else None
        if value not in (None, ''):
            return str(value)
    return None


def prior_rounds(runs, item, ref):
    """Count real implement runs for the item (and PR/branch when named); refusals,
    dry runs and same-round continuations are not rounds."""
    return sum(1 for r in runs
               if r.get('role') == 'implement' and r.get('item_key') == item
               and r.get('status') not in ('refused', 'dry_run')
               and not r.get('continuation')
               and (ref is None or r.get('round_ref') in (None, ref)))


def replan_leg(service, card):
    """Strongest planning leg chosen by the route table; falls back to the fixed order."""
    try:
        plan = service._route(card, 'research')
        return plan.first_leg.id, [leg.id for leg in plan.legs]
    except Exception:
        return None, list(FALLBACK_REPLAN_ORDER)


def check_round_cap(service, card, role):
    item = card.get('item_key') or card.get('item') if isinstance(card, dict) else None
    if role != 'implement' or not item:
        return
    from .stall_watchdog import read_runs
    ref = round_ref(card)
    done = prior_rounds(read_runs(service.state), item, ref)
    if done < MAX_ROUNDS:
        return
    leg, order = replan_leg(service, card)
    raise RoundCapRefused(dict(
        item=item, pr=ref, rounds=done, max_rounds=MAX_ROUNDS,
        findings_source=card.get('findings_source') or ('card.findings' if card.get('findings') else 'review records of the PR'),
        replan_leg=leg, replan_leg_order=order,
        replan_card=dict(
            role='research', item_key=item, pr=ref,
            task=f'Replan {item}: {done} implement rounds did not merge. Read the PR and open findings, '
                 'close the PR, define slices small enough for one round each, and file them as keeper items. '
                 'Do not implement.')))
