"""Cost-first route ordering and catalog tier projection."""


def review_key(leg, tier_idx, pos):
    from .route_policy import review_priority
    return (tier_idx, review_priority(leg), pos)


def catalog_tiers(legs, rows):
    """Project explicit catalog costs, never admission or writer permissions."""
    from dataclasses import replace
    from .legs import TIERS
    tiers = {row.get('id', row.get('model_id')): row.get('tier') for row in rows}
    # Gemini Flash uses the catalog's free allocation, not AgY's default pool.
    from plangraph.routing_support.catalog_seed import SEED_QUOTA_POOLS
    tiers['gemini-3.8-flash'] = next(p['tier'] for p in SEED_QUOTA_POOLS
                                   if p['pool_id'] == 'agy-gemini-flash')
    return tuple(replace(leg, tier=tiers[leg.model])
                 if tiers.get(leg.model) in TIERS else leg for leg in legs)
