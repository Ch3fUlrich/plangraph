"""Cost-first route ordering and catalog tier projection."""

_AMBER_LEG_ID = 'opencode-console-muse-spark'


def review_key(leg, tier_idx, pos, *, measured=False):
    from .route_policy import review_priority
    return (tier_idx, not measured, 0 if measured else review_priority(leg), pos)


def catalog_tiers(legs, rows):
    """Project explicit catalog costs, never admission or writer permissions."""
    from dataclasses import replace
    from .legs import TIERS
    tiers = {row.get('id') or row.get('model_id'): row.get('tier') for row in rows}
    for row in rows:
        provider = row.get('provider_id') or row.get('harness')
        if provider and row.get('upstream_id'):
            tiers.setdefault(f"{provider}/{row['upstream_id']}", row.get('tier'))
    # Harness-local model names resolve through their catalog provider prefix.
    def tier(leg):
        catalog_id = leg.model if '/' in leg.model else f'{leg.harness}/{leg.model}'
        return tiers.get(catalog_id)
    return tuple(replace(leg, tier=tier(leg))
                 if tier(leg) in TIERS else leg for leg in legs)


def payment_tier(leg, role):
    """Classify supplied raw or catalog-projected legs; unknown costs stay put."""
    from .legs import TIERS
    if role in ('implement', 'review') and getattr(leg, 'id', None) == _AMBER_LEG_ID:
        return 'paid'
    tier = getattr(leg, 'tier', None)
    return tier if tier in TIERS else None


def payment_order(legs, role, *, order_class=None, writer_families=(), diff_tokens=None, verdicts=None):
    """Pure post-ranking subscription/paid barrier for implement and review.

    Designated implement writers stay first, regardless of their catalog tiers.
    Only after that prefix must every subscription-class leg precede paid legs.
    A trial or free slot keeps its position unless it is a writer-prefix leg.
    Same-payment-tier ranking stays stable, apart from Amber before paid DeepSeek.
    Unknown payment classes retain their original slots. No admission or I/O.
    An explicit order_class selects D-5696/D-5706 catalog order instead; callers
    supply already-admitted legs, writer families, diff size and cheaper verdicts.
    """
    from .legs import CX_EFFORTS, TIERS, WRITER_ORDER
    from .route_policy import implement_key
    ordered = list(legs)
    if order_class is not None:
        from plangraph.routing_support.catalog_seed import (
            IMPLEMENT_SONNET_REFUSAL, LEGACY_ROUTE_REPLACEMENTS,
            OPERATOR_ROUTE_ORDERS, OPERATOR_REVIEW_CHEAP_LEGS)
        from dataclasses import replace
        from .seats import seat_family, writer_parts
        if order_class not in ('standard', 'high', 'complex'):
            raise ValueError('unknown order class: ' + str(order_class))
        for leg in ordered:
            for legacy, replacement in LEGACY_ROUTE_REPLACEMENTS.items():
                if any(legacy in value.lower() for value in (leg.id, leg.model)):
                    raise ValueError('legacy model refused: ' + leg.model + '; use ' + replacement)
        if role not in OPERATOR_ROUTE_ORDERS:
            raise ValueError('unknown ordered role: ' + str(role))
        table = OPERATOR_ROUTE_ORDERS[role]
        table = table[order_class] if role == 'review' else table
        writers = writer_parts(writer_families) if role == 'review' else set()
        if role == 'review' and not writers:
            raise ValueError('review needs a writer family')
        if diff_tokens is not None and (type(diff_tokens) is not int or diff_tokens < 0):
            raise ValueError('diff_tokens must be a nonnegative integer')
        if len({leg.id for leg in ordered}) != len(ordered):
            raise ValueError('duplicate route leg id')
        effective_role = 'orchestrate' if role == 'planning' else role
        by_id = {leg.id: leg for leg in ordered if effective_role in leg.roles}
        if role == 'implement':
            for leg in ordered:
                if 'sonnet' in (leg.id + leg.model).lower() and leg.harness == 'claude':
                    raise ValueError(IMPLEMENT_SONNET_REFUSAL)
                if (leg.id in by_id and leg.id == 'agy-sonnet-5.5'
                        and leg.harness != 'claude' and leg.model == 'claude-sonnet-5-5-high'):
                    by_id.setdefault('agy-claude-sonnet-5-5-high', leg)
        if role == 'review':
            by_id = {id: leg for id, leg in by_id.items() if seat_family(leg.family) not in writers}
            if diff_tokens is None or diff_tokens >= 100000:
                by_id.pop('claude-haiku-5.5', None)
            votes = {id: verdict for id, verdict in (verdicts or {}).items()
                     if id in OPERATOR_REVIEW_CHEAP_LEGS and id in by_id and verdict in ('PASS', 'FIX')}
            if len(votes) < 2 or len(set(votes.values())) < 2:
                by_id.pop('muse-ember-max', None)
        return tuple(replace(by_id[id], effort=(id.rsplit('-', 1)[-1]
                     if id.rsplit('-', 1)[-1] in CX_EFFORTS else
                     'xhigh' if id == 'claude-haiku-5.5' else 'medium'))
                     for id in table if id in by_id)
    if role not in ('implement', 'review'):
        return tuple(ordered)
    prefix_slots = set()
    if role == 'implement':
        movable = [i for i, leg in enumerate(ordered)
                   if payment_tier(leg, role) in ('subscription', 'paid')
                   or (payment_tier(leg, role) in ('trial', 'free')
                       and getattr(leg, 'id', None) in WRITER_ORDER)]
        prefix = sorted(((i, leg) for i, leg in enumerate(ordered)
                         if payment_tier(leg, role) in TIERS and getattr(leg, 'id', None) in WRITER_ORDER),
                        key=lambda p: implement_key(p[1], TIERS.index(payment_tier(p[1], role)), p[0]))
        positions = {i for i, _ in prefix}
        ranked = [leg for _, leg in prefix] + [ordered[i] for i in movable if i not in positions]
        for i, leg in zip(movable, ranked):
            ordered[i] = leg
        prefix_slots = set(movable[:len(prefix)])
    slots = [i for i, leg in enumerate(ordered)
             if i not in prefix_slots and payment_tier(leg, role) in ('subscription', 'paid')]
    subscriptions = [ordered[i] for i in slots if payment_tier(ordered[i], role) == 'subscription']
    paid = [ordered[i] for i in slots if payment_tier(ordered[i], role) == 'paid']
    amber = next((leg for leg in paid if getattr(leg, 'id', None) == _AMBER_LEG_ID), None)
    deepseek = next((i for i, leg in enumerate(paid) if getattr(leg, 'family', None) == 'deepseek'), None)
    if amber is not None and deepseek is not None and paid.index(amber) > deepseek:
        paid.remove(amber)
        paid.insert(deepseek, amber)
    result = list(ordered)
    for i, leg in zip(slots, subscriptions + paid):
        result[i] = leg
    return tuple(result)
