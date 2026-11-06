"""Process-wide leg-state boundary and next-tick service ordering."""
from dataclasses import replace
from functools import lru_cache
import os


def enabled():
    return os.environ.get('PLANGRAPH_OMNIROUTE_LEG_STATE') != '0'


@lru_cache(maxsize=1)
def default_reader():
    from .leg_state import LegAdapter, _default_store
    from .omniroute_state import from_env
    return LegAdapter(_default_store(), from_env(), enabled=enabled())


def reader_failed(status):
    return status.get('reason') in ('unknown', 'unreachable', 'stale')


def service_plan(service, **kwargs):
    from plangraph.planner.routing import plan_spawn
    if not enabled():
        kwargs.setdefault('leg_reader', service.leg_reader)
        return plan_spawn(**kwargs)
    reader = service.hung.reader if service.hung.enabled else service.leg_reader
    reasons = {}
    def read(model, **scope):
        status = reader.leg_status(model, **scope) if hasattr(reader, 'leg_status') else reader(model, **scope)
        if getattr(reader, 'enabled', False) and reader_failed(status):
            reasons[model] = status['reason']
            return reader.store.leg_status(model, **scope)
        return status
    kwargs.setdefault('leg_reader', read)
    plan = plan_spawn(**kwargs)
    if not getattr(reader, 'enabled', False):
        return plan
    from plangraph.spawner.leg_probe import key
    legs = {key(leg): leg for leg in plan.legs}
    ordered = tuple(legs[mid] for mid in service.hung.rank(list(legs)))
    legs_by_id = {leg.id: leg for leg in plan.legs}
    details = tuple(dict(c, leg_state_reason=reasons[legs_by_id[c['leg']].model])
                    if c['leg'] in legs_by_id and legs_by_id[c['leg']].model in reasons else c
                    for c in plan.candidates)
    return replace(plan, legs=ordered, candidates=details)


def service_reader(service, reader):
    from plangraph.spawner.hung_capture import HungBoundary
    reader = default_reader() if reader is None else reader
    service.hung = HungBoundary(reader, sample=lambda r: {}, identity=lambda p, i: False,
                               cancel=lambda r: False, fallthrough=lambda r: None)
    return reader
