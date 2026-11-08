"""Host-local cooldown evidence derived from existing private run records."""
import json
import os
import socket
import time

LEG_DOWN_WINDOW_S = 3600
DOWN_REASONS = frozenset(('not_served_on_host', 'credit_limit', 'usage_limit'))


def measured_down(state, *, now=None):
    """Return leg -> reason/time; missing or malformed records carry no evidence."""
    now = time.time() if now is None else now
    host = os.environ.get('PLANGRAPH_HOST') or socket.gethostname()
    down = {}
    from plangraph.routing_support.leg_state import leg_status, RESET_CAP_S, _default_store
    window = max(RESET_CAP_S, LEG_DOWN_WINDOW_S) if _default_store().path.exists() else LEG_DOWN_WINDOW_S
    for path in (state / 'runs').glob('run-*/record.json'):
        try:
            if not 0 <= now - path.stat().st_mtime < window:
                continue
            record = json.loads(path.read_text())
            ended = record.get('ended')
            if record.get('host') != host or not 0 <= now - ended < window:
                continue
            attempts = record.get('attempts') or []
            for attempt in attempts:
                if attempt.get('status') not in ('failed', 'skipped'):
                    continue
                reason = attempt.get('down_reason')
                leg = attempt.get('leg')
                model = leg.get('model') if isinstance(leg, dict) else None
                # Same key as spawner.leg_probe.key (the planner must not import the spawner).
                booking_key = (('agy/' + model if leg.get('harness') == 'agy' and '/' not in model else model)
                               if isinstance(model, str) else leg.get('id') if isinstance(leg, dict) else leg)
                leg = leg.get('id') if isinstance(leg, dict) else leg
                if not isinstance(leg, str) or not leg or reason not in DOWN_REASONS:
                    continue
                if now - ended >= LEG_DOWN_WINDOW_S and leg_status(booking_key, now=now)['state'] != 'limited':
                    continue
                if leg and reason in DOWN_REASONS and ended > down.get(leg, {}).get('time', 0):
                    down[leg] = dict(reason=reason, time=ended)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            continue
    return down
