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


def payment_order(legs, role):
    """Pure post-ranking subscription/paid barrier for implement and review.

    Preserve the designated implement prefix except across the subscription/paid
    boundary. Other trial/free slots and same-payment-tier ranking stay stable.
    Unknown payment classes retain their original slots. No admission or I/O.
    """
    from .legs import TIERS, WRITER_ORDER
    from .route_policy import implement_key
    ordered = list(legs)
    if role not in ('implement', 'review'):
        return tuple(ordered)
    if role == 'implement':
        movable = [i for i, leg in enumerate(ordered)
                   if payment_tier(leg, role) in ('subscription', 'paid')
                   or (payment_tier(leg, role) in ('trial', 'free') and leg.id in WRITER_ORDER)]
        prefix = sorted(((i, leg) for i, leg in enumerate(ordered)
                         if payment_tier(leg, role) in TIERS and leg.id in WRITER_ORDER),
                        key=lambda p: implement_key(p[1], TIERS.index(payment_tier(p[1], role)), p[0]))
        positions = {i for i, _ in prefix}
        ranked = [leg for _, leg in prefix] + [ordered[i] for i in movable if i not in positions]
        for i, leg in zip(movable, ranked):
            ordered[i] = leg
    slots = [i for i, leg in enumerate(ordered)
             if payment_tier(leg, role) in ('subscription', 'paid')]
    subscriptions = [ordered[i] for i in slots if payment_tier(ordered[i], role) == 'subscription']
    paid = [ordered[i] for i in slots if payment_tier(ordered[i], role) == 'paid']
    amber = next((leg for leg in paid if leg.id == _AMBER_LEG_ID), None)
    deepseek = next((i for i, leg in enumerate(paid) if getattr(leg, 'family', None) == 'deepseek'), None)
    if amber is not None and deepseek is not None and paid.index(amber) > deepseek:
        paid.remove(amber)
        paid.insert(deepseek, amber)
    result = list(ordered)
    for i, leg in zip(slots, subscriptions + paid):
        result[i] = leg
    return tuple(result)
