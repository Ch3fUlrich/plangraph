"""Cost-first route ordering and catalog tier projection."""


def review_key(leg, tier_idx, pos):
    from .route_policy import review_priority
    return ("haiku" in leg.model.lower(), tier_idx, review_priority(leg), pos)


def catalog_tiers(legs, rows):
    """Project explicit catalog costs, never admission or writer permissions."""
    from dataclasses import replace
    from .legs import TIERS
    tiers = {row.get('id') or row.get('model_id'): row.get('tier') for row in rows}
    result = []
    for leg in legs:
        tier = tiers.get(leg.model, tiers.get(f'{leg.harness}/{leg.model}'))
        result.append(replace(leg, tier=tier) if tier in TIERS else leg)
    return tuple(result)
