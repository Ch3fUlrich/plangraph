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
    states = {'limited', 'down', 'expired'}
    policy = {'banned_family', 'off_family', 'paused_harness', 'excluded_family',
              'inline_only_needs_diff', 'no_verdict_seat', 'explicit_only',
              'review_route_excluded', 'low_risk_only', 'claude_requires_gpt_and_hydrogen_limits',
              'claude_requires_codex_limited'}
    unavailable_legs = {'not_listed', 'provider_inactive', 'unreachable'}
    limited = lambda r: r in states or r.startswith(('quota_until ', 'limited_until '))
    return any(limited(r) for r in reasons) and all(
        limited(r) or r in policy or r in unavailable_legs or r.startswith('disabled:') for r in reasons)


def read_leg_model(read, leg, model, **scope):
    """Read availability in the selected harness's quota and discovery scope."""
    harness = getattr(leg, "harness", None)
    foreign = harness not in (None, "agy")
    if harness == "codex" or (foreign and model.startswith(("agy:", "agy/"))):
        scope["harness"] = harness
    state = read(model, **scope)
    evidence = state.get("evidence") or {}
    if foreign and evidence.get("quota_scope") == "account" and evidence.get("harness") == "agy":
        return {"state": "ok", "evidence": None}
    return state


def require_available(leg, reader, card):
    """Overrides cannot bypass the same state boundary used by routing."""
    from .leg_state import leg_status
    from plangraph.planner.routing import SpawnRefused
    from .leg_state import leg_key as key
    read = getattr(reader, 'leg_status', reader) or leg_status
    model = key(leg)
    state = read_leg_model(read, leg, model, account=card.get('account'), combo=card.get('combo'))
    if unavailable(state):
        reason = agy_limit_reason(model, state) if getattr(leg, "harness", None) == "agy" else None
        raise SpawnRefused((reason or 'leg ' + state['state']) + ': ' + leg.id)
