"""Provider-stop recording and fallback selection for spawn plans."""
from plangraph.routing_errors import SpawnRefused
from plangraph.routing_support.liveness import unavailable


def next_leg_after_provider_stop(plan, leg_id, status, body, headers, caller, *,
                                 store=None, now=None, account=None, combo=None):
    """Consume an actual provider stop; unrelated failures never suppress a leg."""
    from plangraph.routing_support import leg_state
    state = store or leg_state
    by_id = {leg.id: leg for leg in plan.legs}
    if not isinstance(leg_id, str):
        raise SpawnRefused('leg id must be a string')
    if leg_id not in by_id:
        raise ValueError("leg not in plan")
    if type(status) is not int or status not in (429, 402) or not isinstance(body, str) or not body.strip():
        return by_id[leg_id]
    state.record_provider_response(by_id[leg_id].model, status, body, headers, caller,
                                   now=now, account=account, combo=combo)
    start = list(by_id).index(leg_id) + 1
    for leg in plan.legs[start:]:
        st = state.leg_status(leg.model, now=now, account=account, combo=combo)
        if leg.harness == "agy" and not unavailable(st) and "/" not in leg.model:
            alt = state.leg_status("agy/" + leg.model, now=now, account=account, combo=combo)
            if unavailable(alt):
                continue
        if not unavailable(st):
            return leg
    return None
