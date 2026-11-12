"""Cost-first route ordering and catalog tier projection."""


def review_key(leg, tier_idx, pos, *, measured=False):
    from .route_policy import review_priority
    return (tier_idx, paid_priority(leg),
            not measured, 0 if measured else review_priority(leg), pos)


def paid_priority(leg):
    """D-5240: paid Muse precedes paid DeepSeek, even after local reranking."""
    from .legs import MUSE_AMBER
    if getattr(leg, 'tier', None) != 'paid':
        return 0
    return 0 if leg.id == MUSE_AMBER else 1


def catalog_tiers(legs, rows, role=None):
    """Project qualified catalog costs uniformly across legs and roles, not permissions."""
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
