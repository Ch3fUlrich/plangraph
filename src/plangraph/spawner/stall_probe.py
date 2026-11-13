"""Host run evidence and bounded probe settings."""
import json
import math
import os
from pathlib import Path

def probe_setting(name, default, maximum):
    try:
        value = float(os.environ.get(name, default))
        return min(value, maximum) if math.isfinite(value) and value >= 0 else default
    except ValueError:
        return default

def probe_run(state, record, now, *, dry_run=False, hung=None):
    """Independent host sampling; missing evidence never authorizes cancellation."""
    if hung is not None and hung.enabled:
        return hung.probe(state, record, now, dry_run=dry_run)
    import asyncio
    import math
    from .private_io import atomic_write_private
    event = dict(rule='unknown')
    try:
        event.update(run_id=record.get('run_id'), leg=record.get('leg'), role=record.get('role'),
                     age=None, seconds_silent=None, last_line=None)
        directory = Path(state) / 'runs' / record['run_id']
        finished = record.get('status') not in ('running', 'queued', None)
        if finished:
            ended = record.get('ended')
            if (isinstance(ended, bool) or not isinstance(ended, (int, float))
                    or not math.isfinite(ended) or not 0 <= now - ended <= probe_setting(
                        'PLANGRAPH_PROBE_HORIZON_S', 86400, 604800)):
                return None
        log = directory / 'live.log'
        try:
            with log.open(encoding='utf-8') as stream:
                stream.seek(max(0, log.stat().st_size - 4096))
                lines = stream.read().splitlines()
            event.update(seconds_silent=max(0, now - log.stat().st_mtime),
                         last_line=lines[-1][:200] if lines else '')
        except (OSError, UnicodeError):
            pass
        try:
            started = record['started']
            if math.isfinite(started):
                event['age'] = max(0, now - started)
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        if finished:
            message = str(record.get('final_message', '')) + ' ' + (event['last_line'] or '')
            if record.get('role') == 'implement' and (record.get('reason') == 'writer_no_changes'
                    or 'running in the background' in message.lower()):
                return dict(event, rule='run-ended-no-commit')
            return None
        if record.get('status') is None and event['age'] is None:
            return event
        if record.get('status') == 'running':
            pid = record.get('pid')
            if not isinstance(pid, int) or pid <= 0 or os.name == 'nt':
                return event
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                event['rule'] = 'run-orphan'
                record = dict(record, status='failed', reason='orphaned', ended=now, exit_code=1)
                if not dry_run:
                    atomic_write_private(directory / 'record.json', json.dumps(record))
                return event
        silent = event['seconds_silent']
        from .run_watchdog_limits import thresholds
        limit = thresholds(record.get('card'), role=record.get('role'))['stall_s']
        if not math.isfinite(limit) or limit <= 0:
            return event
        if silent is None:
            if record.get('status') == 'running' and event['age'] is not None and event['age'] < limit:
                return None
            return event
        if record.get('status') == 'running':
            if silent < limit:
                return None
            event['rule'] = 'run-silent'
            event['episode'] = log.stat().st_mtime
            event.update(stalled_at=event['episode'] + limit, noticed_at=now)
            if silent >= 2 * limit and not dry_run:
                from .runner import spawn_service
                result = asyncio.run(spawn_service(state=state).cancel(record['run_id']))
                event['cancelled'] = result.get('status') == 'cancelled'
            return event
        return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return event


class ToolLoop:
    """Three consecutive tool invocations with identical names and arguments.

    Tool IDs deduplicate streaming updates; only the digest is retained.
    Text and tool results are never interpreted as invocations.
    """
    def __init__(self):
        from collections import deque
        self.seen = set()
        self.order = deque()
        self.last, self.count = None, 0

    def feed(self, line):
        import hashlib
        try:
            frame = json.loads(line)
        except (ValueError, TypeError):
            return False
        if not isinstance(frame, dict):
            return False
        kind = frame.get('type')
        calls = []
        if kind == 'tool_use' and isinstance(frame.get('part'), dict):
            part = frame['part']
            state = part.get('state')
            if isinstance(state, dict) and 'input' in state:
                calls = [(part.get('callID'), part.get('tool'), state['input'])]
        elif kind in ('tool_call', 'function_call'):
            calls = [(frame.get('id'), frame.get('name'), frame.get('arguments'))]
        elif kind == 'assistant' and isinstance(frame.get('message'), dict):
            content = frame['message'].get('content')
            calls = [(part.get('id'), part.get('name'), part.get('input'))
                     for part in content if isinstance(part, dict) and part.get('type') == 'tool_use'] if isinstance(content, list) else []
        elif kind in ('item.started', 'item.completed') and isinstance(frame.get('item'), dict):
            item = frame['item']
            if item.get('type') in ('tool_call', 'function_call'):
                calls = [(item.get('id'), item.get('name'), item.get('arguments'))]
            elif item.get('type') == 'command_execution':
                calls = [(item.get('id'), 'command_execution', item.get('command'))]
        found = False
        for identity, name, arguments in calls:
            if not isinstance(name, str) or not name:
                continue
            if isinstance(identity, str) and identity:
                if identity in self.seen:
                    continue
                self.seen.add(identity)
                self.order.append(identity)
                if len(self.order) > 1024:
                    self.seen.remove(self.order.popleft())
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    pass
            digest = hashlib.sha256(json.dumps([name, arguments], sort_keys=True, separators=(',', ':')).encode()).digest()
            self.count = self.count + 1 if digest == self.last else 1
            self.last = digest
            found |= self.count >= 3
        return found
