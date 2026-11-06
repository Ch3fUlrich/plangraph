"""Host-local cooldown evidence derived from existing private run records."""
import json
import os
import re
import socket
import time

LEG_DOWN_WINDOW_S = 3600
_DOWN = re.compile(r'credit(?: usage)? limit|usage limit|model not available|not_served_on_host', re.I)


def measured_down(state, *, now=None):
    """Return leg -> reason/time; missing or malformed records carry no evidence."""
    now = time.time() if now is None else now
    host = os.environ.get('PLANGRAPH_HOST') or socket.gethostname()
    down = {}
    for path in (state / 'runs').glob('run-*/record.json'):
        try:
            record = json.loads(path.read_text())
            ended = record.get('ended')
            if record.get('host') != host or not 0 <= now - ended < LEG_DOWN_WINDOW_S:
                continue
            attempts = record.get('attempts') or [dict(leg=record.get('chosen_leg'),
                status=record.get('status'), skip_reason=record.get('reason') or record.get('error'))]
            for attempt in attempts:
                if attempt.get('status') not in ('failed', 'completed', 'skipped'):
                    continue
                reason = attempt.get('skip_reason') or ''
                leg = attempt.get('leg')
                leg = leg.get('id') if isinstance(leg, dict) else leg
                if leg and _DOWN.search(reason) and ended > down.get(leg, {}).get('time', 0):
                    down[leg] = dict(reason=reason, time=ended)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return down
