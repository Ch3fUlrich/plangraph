"""Cost-first route ordering and catalog tier projection."""

_RAW_TIER = object()


def review_key(leg, tier_idx, pos, *, measured=False):
    from .route_policy import review_priority
    return (tier_idx, not measured, 0 if measured else review_priority(leg), pos)


def catalog_tiers(legs, rows, role=None):
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
        return payment_tier(leg, role, tiers.get(catalog_id))
    return tuple(replace(leg, tier=tier(leg))
                 if tier(leg) in TIERS else leg for leg in legs)


def _amber(leg):
    from .legs import AMBER_MODEL
    return (getattr(leg, 'harness', None), getattr(leg, 'model', None)) == ('opencode', AMBER_MODEL)


def payment_tier(leg, role, tier=_RAW_TIER):
    """One role-aware classification for raw legs and optional catalog prices."""
    if role in ('implement', 'review') and _amber(leg):
        return 'paid'
    return getattr(leg, 'tier', None) if tier is _RAW_TIER else tier


def payment_order(legs, role):
    """Pure post-ranking barrier; review changes only when Amber is present.

    Preserve the designated implement prefix except across the subscription/paid
    boundary. Other trial/free slots and same-payment-tier ranking stay stable.
    """
    from .legs import TIERS, WRITER_ORDER
    from .route_policy import implement_key
    ordered = tuple(legs)
    if role not in ('implement', 'review') or (role == 'review' and not any(map(_amber, ordered))):
        return ordered
    if role == 'implement':
        prefix = sorted(((i, leg) for i, leg in enumerate(ordered)
                         if leg.id in WRITER_ORDER and getattr(leg, 'tier', None) in TIERS),
                        key=lambda p: implement_key(p[1], TIERS.index(payment_tier(p[1], role)), p[0]))
        positions = {i for i, _ in prefix}
        ordered = tuple(leg for _, leg in prefix) + tuple(
            leg for i, leg in enumerate(ordered) if i not in positions)
    slots = [i for i, leg in enumerate(ordered) if getattr(leg, 'tier', None) in TIERS
             and payment_tier(leg, role) in ('subscription', 'paid')]
    subscriptions = [ordered[i] for i in slots if payment_tier(ordered[i], role) == 'subscription']
    paid = [ordered[i] for i in slots if payment_tier(ordered[i], role) == 'paid']
    amber = next((leg for leg in paid if _amber(leg)), None)
    deepseek = next((i for i, leg in enumerate(paid) if getattr(leg, 'family', None) == 'deepseek'), None)
    if amber is not None and deepseek is not None and paid.index(amber) > deepseek:
        paid.remove(amber)
        paid.insert(deepseek, amber)
    result = list(ordered)
    for i, leg in zip(slots, subscriptions + paid):
        result[i] = leg
    return tuple(result)


def next_cost_leg(legs, tried, last, *, role=None):
    """Budget-exhausted successor only; retry/provider-stop rules stay with callers."""
    from .legs import TIERS
    spent = {*tried, last.id}
    last_tier = TIERS.index(payment_tier(last, role))
    for leg in payment_order(legs, role):
        if leg.id in spent:
            continue
        tier = TIERS.index(payment_tier(leg, role))
        if tier > last_tier:
            return ('promote', leg)
        if role in ('implement', 'review') and tier == last_tier:
            return ('rotate', leg)
    return ('park', None)
