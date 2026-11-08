"""Cost-first route ordering and catalog tier projection."""


def review_key(leg, tier_idx, pos):
    from .route_policy import review_priority
    return (tier_idx, review_priority(leg), pos)


def catalog_tiers(legs, rows):
    """Project explicit catalog costs, never admission or writer permissions."""
    from dataclasses import replace
    from .legs import TIERS
    tiers = {row.get('id', row.get('model_id')): row.get('tier') for row in rows}
    # Harness-local model names resolve through their catalog provider prefix.
    for leg in legs:
        catalog_id = f'{leg.harness}/{leg.model}'
        if leg.model not in tiers and catalog_id in tiers: tiers[leg.model] = tiers[catalog_id]
    return tuple(replace(leg, tier=tiers[leg.model])
                 if tiers.get(leg.model) in TIERS else leg for leg in legs)
