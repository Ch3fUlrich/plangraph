"""Shared route exclusion predicate for quota and liveness evidence."""


def unavailable(state):
    return state['state'] in ('limited', 'down', 'expired')


def provider_exhausted(skipped):
    """Policy-only skips do not hide exhausted eligible provider capacity."""
    reasons = [s['reason'] for s in skipped]
    states = {'limited', 'down', 'expired'}
    policy = {'banned_family', 'off_family', 'paused_harness', 'excluded_family',
              'inline_only_needs_diff', 'no_verdict_seat', 'explicit_only'}
    return any(r in states for r in reasons) and all(
        r in states | policy or r.startswith(('disabled:', 'legacy model refused:')) for r in reasons)


def require_available(leg, reader, card):
    """Overrides cannot bypass the same state boundary used by routing."""
    from .leg_state import leg_status
    from plangraph.planner.routing import SpawnRefused
    read = getattr(reader, 'leg_status', reader) or leg_status
    state = read(leg.model, account=card.get('account'), combo=card.get('combo'))
    if unavailable(state):
        raise SpawnRefused('leg ' + state['state'] + ': ' + leg.id)
