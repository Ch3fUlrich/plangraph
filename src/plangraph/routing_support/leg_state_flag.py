"""Process-wide adapter and declared-state fallback for service plans."""
from dataclasses import replace
from functools import lru_cache
import logging
import os
from pathlib import Path
import re
from plangraph.routing_errors import SpawnRefused


def enabled():
    return os.environ.get('PLANGRAPH_OMNIROUTE_LEG_STATE') != '0'


def read_snapshot(reader):
    """Retry the provider/discovery boundary once, never cache a partial refresh."""
    from .omniroute_state import OmniRouteState
    for attempt in range(2):
        try:
            states = reader.provider.snapshot()
            if isinstance(reader.provider, OmniRouteState) and not states:
                raise ValueError('empty provider snapshot')
            return states, {reader.identity(mid) for mid in (
                reader.models() if callable(reader.models) else reader.models)}
        except Exception as exc:
            if attempt:
                raise
            logging.getLogger(__name__).warning(
                'Leg state snapshot read failed (%s); retrying once', type(exc).__name__)


def snapshot_detail(reader, scrub):
    """Scrub secrets before removing locations, so paths cannot split a secret."""
    error = getattr(reader, '_snapshot_error', None)
    detail = ': '.join(error) if error else 'SnapshotError: detail unavailable'
    detail = scrub(detail)
    detail = detail.replace(str(Path.home()), '[home]')
    return re.sub(r'''(?:[A-Za-z]:[\\/]|/|~[/\\])[^\r\n'";,]+''', '[path]', detail)


def snapshot_admission(reader, model, scrub, **scope):
    """Only failed snapshots are recoverable through liveness evidence."""
    status = reader.leg_status(model, **{k: v for k, v in scope.items() if v is not None})
    reason = status.get('reason') or status['state']
    if reason.strip().lower() == 'unknown' or reason.startswith('leg_state_snapshot_error'):
        return 'leg_state_snapshot_error: ' + ' '.join(snapshot_detail(reader, scrub).split())
    return reason if status['state'] != 'ok' else None


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
    service.leg_reader = reader = default_reader() if reader is None and enabled() else reader
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
