"""Process-wide adapter and declared-state fallback for service plans."""
from dataclasses import replace
from functools import lru_cache
import os
from plangraph.routing_errors import SpawnRefused


def enabled():
    return os.environ.get('PLANGRAPH_OMNIROUTE_LEG_STATE') != '0'


@lru_cache(maxsize=1)
def default_reader():
    from .leg_state import LegAdapter, _default_store
    from .omniroute_state import from_env
    reader = LegAdapter(_default_store(), from_env(), enabled=enabled())
    original = reader.leg_status
    def status(model, **scope):
        result = original(model, **scope)
        if result.get('reason') in ('unknown', 'unreachable', 'stale'):
            return dict(reader.store.leg_status(model, **scope), reason=result['reason'])
        return result
    reader.leg_status = status
    return reader


def initialize(service, reader, boundary):
    if reader is None:
        from .leg_state import _default_store
        cached = default_reader()
        store = _default_store()
        if cached.store.path != store.path:
            cached.store, cached._at = store, None
    service.leg_reader = reader = default_reader() if reader is None else reader
    service.hung = boundary(reader, sample=lambda r: {}, identity=lambda p, i: False,
                            cancel=lambda r: False, fallthrough=lambda r: None)
    return reader


def service_plan(service, **kwargs):
    from plangraph.planner.routing import plan_spawn
    reader = getattr(service, 'leg_reader', None)
    if not getattr(reader, 'enabled', False):
        return plan_spawn(**kwargs, leg_reader=reader)
    reasons = {}
    def read(model, **scope):
        status = reader.leg_status(model, **scope)
        if status.get('reason') in ('unknown', 'unreachable', 'stale'):
            reasons[model] = status['reason']
            return reader.store.leg_status(model, **scope)
        return status
    plan = plan_spawn(**kwargs, leg_reader=read)
    by_id = {leg.id: leg.model for leg in plan.legs}
    details = tuple(dict(c, leg_state_reason=reasons[by_id[c['leg']]])
                    if by_id.get(c['leg']) in reasons else c for c in plan.candidates)
    models = service.hung.rank([leg.model for leg in plan.legs])
    ordered = tuple(sorted(plan.legs, key=lambda leg: models.index(leg.model)))
    return replace(plan, legs=ordered, candidates=details)
