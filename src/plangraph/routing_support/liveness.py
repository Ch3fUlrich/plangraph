"""Shared route exclusion predicate for quota and liveness evidence."""
import time


def agy_limit_reason(model, state):
    reset = (state.get('evidence') or {}).get('reset_at')
    if model.startswith(('agy:', 'agy/')) and state['state'] == 'limited' and reset:
        return 'limited_until ' + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(reset))


def unavailable(state):
    return state['state'] in ('limited', 'down', 'expired')


def provider_exhausted(skipped):
    """Check whether the remaining eligible capacity is exhausted."""
    reasons = [s['reason'] for s in skipped]
    capacity_reasons = {'limited', 'down', 'expired', 'provider_busy', 'model_unavailable'}
    policy = {'banned_family', 'off_family', 'paused_harness', 'excluded_family',
              'inline_only_needs_diff', 'no_verdict_seat', 'explicit_only',
              'review_route_excluded', 'low_risk_only', 'claude_requires_gpt_and_hydrogen_limits',
              'claude_requires_codex_limited'}
    unavailable_legs = {'not_listed', 'provider_inactive', 'unreachable'}
    exhausted = lambda r: r in capacity_reasons or r.startswith(('quota_until ', 'limited_until '))
    return any(exhausted(r) for r in reasons) and all(
        exhausted(r) or r in policy or r in unavailable_legs or r.startswith('disabled:') for r in reasons)


def read_leg_model(read, leg, model, **scope):
    """Read availability in the selected harness's quota and discovery scope."""
    harness = getattr(leg, "harness", None)
    foreign = harness not in (None, "agy")
    if harness == "codex" or (foreign and model.startswith(("agy:", "agy/"))):
        scope["harness"] = harness
    return read(model, **scope)


def require_available(leg, reader, card):
    """Overrides cannot bypass the same state boundary used by routing."""
    from .leg_state import leg_status
    from .quota_snapshots import skip_reason as quota_skip_reason
    from plangraph.planner.routing import SpawnRefused
    from .leg_state import leg_key as key
    read = getattr(reader, 'leg_status', reader) or leg_status
    model = key(leg)
    state = read_leg_model(read, leg, model, account=card.get('account'), combo=card.get('combo'))
    if unavailable(state):
        reason = quota_skip_reason(state)
        if reason is None and getattr(leg, "harness", None) == "agy":
            reason = agy_limit_reason(model, state)
        raise SpawnRefused((reason or 'leg ' + (state.get('reason') or state['state'])) + ': ' + leg.id)
