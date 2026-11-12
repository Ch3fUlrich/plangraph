"""D-5240: cost tier first; D-4147 role preferences remain within each tier."""


def review_key(leg, tier_idx, pos):
    from .route_policy import review_priority
    return (tier_idx, review_priority(leg), pos)
