"""Process-wide adapter and declared-state fallback for service plans."""
from dataclasses import replace
from functools import lru_cache
import os
from plangraph.routing_errors import SpawnRefused


def enabled():
    return os.environ.get('PLANGRAPH_OMNIROUTE_LEG_STATE') != '0'


def _needs_fallback(status):
    reason = status.get('reason') or ''
    # An unreachable provider in a usable snapshot is an explicit exclusion.
    return (reason in ('unknown', 'stale') or reason.startswith('leg_state_snapshot_error:')
            or (reason == 'unreachable' and status['state'] != 'down'))


@lru_cache(maxsize=1)
def default_reader():
    from .leg_state import LegAdapter, _default_store
    from .quota_snapshots import CACHE_S, QuotaGate, from_env
    reader = LegAdapter(_default_store(), from_env(), enabled=enabled())
    # A local snapshot can change between routing and launch. Cache only the remote API.
    original, gate = reader.leg_status, QuotaGate(ttl_s=0 if os.environ.get('QUOTA_SNAPSHOT_DB') else CACHE_S)
    def status(model, **scope):
        result = original(model, **scope)
        if _needs_fallback(result):
            result = dict(reader.store.leg_status(model, **scope), reason=result['reason'])
        return gate.apply(model, result, scope.get('now'))   # pre-route omniroute quota_snapshots gate
    reader.leg_status = status
    return reader


def initialize(service, reader, boundary):
    service.leg_reader = reader = default_reader() if reader is None and (
        enabled() or os.environ.get('QUOTA_SNAPSHOT_DB')) else reader
    service.hung = boundary(reader)
    return reader


def service_plan(service, **kwargs):
    from plangraph.planner.routing import plan_spawn
    reader = getattr(service, 'leg_reader', None)
    if not enabled() or not getattr(reader, 'enabled', False):
        return plan_spawn(**kwargs, leg_reader=reader)
    reasons = {}
    def read(model, **scope):
        status = reader.leg_status(model, **scope)
        if _needs_fallback(status):
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
