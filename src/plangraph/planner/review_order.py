"""D-4147: mimo first, table order next, Haiku last (independent of cost)."""


def review_key(leg, tier_idx, pos):
    from .route_policy import review_priority
    return (review_priority(leg), pos)
