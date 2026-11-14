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
    from .quota_snapshots import QuotaGate
    reader = LegAdapter(_default_store(), from_env(), enabled=enabled())
    original, gate = reader.leg_status, QuotaGate()
    def status(model, **scope):
        result = original(model, **scope)
        if result.get('reason') in ('unknown', 'unreachable', 'stale') or (result.get('reason') or '').startswith('leg_state_snapshot_error:'):
            result = dict(reader.store.leg_status(model, **scope), reason=result['reason'])
        return gate.apply(model, result, scope.get('now'))   # pre-route omniroute quota_snapshots gate
    reader.leg_status = status
    return reader


def initialize(service, reader, boundary):
    service.leg_reader = reader = default_reader() if reader is None and enabled() else reader
    service.hung = boundary(reader)
    return reader


def live_default_legs(role):
    """Unpinned live legs: D-5696 legacy ids and, for review, D-5648 argon are never routed."""
    from plangraph.planner.routing import DEFAULT_LEGS, plan_spawn
    from .catalog_seed import LEGACY_ROUTE_REPLACEMENTS
    def fenced(leg):
        ident = f'{leg.id}{leg.model}'.lower()
        return any(old in ident for old in LEGACY_ROUTE_REPLACEMENTS) or (role == 'review' and 'argon' in ident)
    return tuple(leg for leg in (plan_spawn.__kwdefaults__ or {}).get('legs', DEFAULT_LEGS) if not fenced(leg))


def service_plan(service, **kwargs):
    from plangraph.planner.routing import plan_spawn
    kwargs.setdefault('legs', live_default_legs(kwargs.get('role')))
    reader = getattr(service, 'leg_reader', None)
    if not enabled() or not getattr(reader, 'enabled', False):
        return plan_spawn(**kwargs, leg_reader=reader)
    reasons = {}
    def read(model, **scope):
        status = reader.leg_status(model, **scope)
        if status.get('reason') in ('unknown', 'unreachable', 'stale'):
            reasons[model] = status['reason']
            store = getattr(reader, 'store', None)
            return store.leg_status(model, **scope) if store is not None else status
        return status
    plan = plan_spawn(**kwargs, leg_reader=read)
    by_id = {leg.id: leg.model for leg in plan.legs}
    details = tuple(dict(c, leg_state_reason=reasons[by_id[c['leg']]])
                    if by_id.get(c['leg']) in reasons else c for c in plan.candidates)
    rank = {key: i for i, key in enumerate(service.hung.rank([leg.id for leg in plan.legs]))}
    ordered = tuple(leg for i, leg in sorted(enumerate(plan.legs),
                    key=lambda row: (rank.get(row[1].id, float("inf")), row[0])))
    return replace(plan, legs=ordered, candidates=details)
