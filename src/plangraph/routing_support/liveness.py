"""Shared route exclusion predicate for quota and liveness evidence."""


def unavailable(state):
    return state['state'] in ('limited', 'down', 'expired')


def require_available(leg, reader, card):
    """Overrides cannot bypass the same state boundary used by routing."""
    from .leg_state import leg_status
    from plangraph.planner.routing import SpawnRefused
    read = getattr(reader, 'leg_status', reader) or leg_status
    state = read(leg.model, account=card.get('account'), combo=card.get('combo'))
    if unavailable(state):
        raise SpawnRefused('leg ' + state['state'] + ': ' + leg.id)
