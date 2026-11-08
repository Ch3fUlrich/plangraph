"""Cost-first route ordering and catalog tier projection."""


def review_key(leg, tier_idx, pos):
    from .route_policy import review_priority
    return (tier_idx, review_priority(leg), pos)


def catalog_tiers(legs, rows):
    """Project explicit catalog costs, never admission or writer permissions."""
    from dataclasses import replace
    from .legs import TIERS
    tiers = {row.get('id') or row.get('model_id'): row.get('tier') for row in rows}
    for row in rows:
        if row.get('harness') and row.get('upstream_id'):
            tiers.setdefault(f"{row['harness']}/{row['upstream_id']}", row.get('tier'))
    # Harness-local model names resolve through their catalog provider prefix.
    def tier(leg):
        catalog_id = leg.model if '/' in leg.model else f'{leg.harness}/{leg.model}'
        return tiers.get(catalog_id, tiers.get(leg.model))
    return tuple(replace(leg, tier=tier(leg))
                 if tier(leg) in TIERS else leg for leg in legs)
