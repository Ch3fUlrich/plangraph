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
    """Six consecutive completed calls with identical arguments AND results.

    Streaming call IDs deduplicate completions. Pending calls and results retain
    only digests; id-less lifecycle starts never count as completed invocations.
    """
    MISSING = object()
    REPEATS = 6

    def __init__(self):
        from collections import deque
        self.seen, self.order, self.pending = set(), deque(), {}
        self.last, self.count = None, 0

    def _observe(self, identity, name=None, arguments=MISSING, result=MISSING):
        import hashlib
        identity = identity if isinstance(identity, str) and identity else None
        call = self.pending.get(identity)
        if isinstance(name, str) and name and arguments is not self.MISSING:
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    pass
            call = hashlib.sha256(json.dumps([name, arguments], sort_keys=True).encode()).digest()
        if result is self.MISSING:
            if identity and call:
                self.pending[identity] = call
                if len(self.pending) > 1024:
                    self.pending.pop(next(iter(self.pending)))
            return False
        if call is None or identity in self.seen:
            return False
        if identity:
            self.pending.pop(identity, None)
            self.seen.add(identity)
            self.order.append(identity)
            if len(self.order) > 1024:
                self.seen.remove(self.order.popleft())
        digest = hashlib.sha256(call + json.dumps(result, sort_keys=True).encode()).digest()
        self.count = self.count + 1 if digest == self.last else 1
        self.last = digest
        return self.count >= self.REPEATS

    def feed(self, line):
        try:
            frame = json.loads(line)
        except (ValueError, TypeError):
            return False
        if not isinstance(frame, dict):
            return False
        def result(part):
            for key in ('output', 'result', 'aggregated_output', 'content'):
                if key in part:
                    return [part[key], part.get('exit_code'), part.get('is_error')]
            return self.MISSING
        kind = frame.get('type')
        observations = []
        if kind == 'tool_use' and isinstance(frame.get('part'), dict):
            part = frame['part']
            state = part.get('state')
            if isinstance(state, dict):
                observations = [(part.get('callID'), part.get('tool'), state.get('input', self.MISSING),
                                 result(state) if state.get('status') in ('completed', 'error') else self.MISSING)]
        elif kind in ('tool_call', 'function_call'):
            observations = [(frame.get('id'), frame.get('name'), frame.get('arguments', self.MISSING), result(frame))]
        elif kind == 'tool_result':
            observations = [(frame.get('tool_call_id') or frame.get('call_id') or frame.get('id'),
                             None, self.MISSING, result(frame))]
        elif kind in ('assistant', 'user') and isinstance(frame.get('message'), dict):
            content = frame['message'].get('content')
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get('type') == 'tool_use':
                    observations.append((part.get('id'), part.get('name'), part.get('input', self.MISSING), self.MISSING))
                elif part.get('type') == 'tool_result':
                    observations.append((part.get('tool_use_id'), None, self.MISSING, result(part)))
        elif kind in ('item.started', 'item.completed') and isinstance(frame.get('item'), dict):
            item = frame['item']
            if item.get('type') in ('tool_call', 'function_call', 'command_execution'):
                command = item.get('type') == 'command_execution'
                observations = [(item.get('id'), 'command_execution' if command else item.get('name'),
                                 item.get('command' if command else 'arguments', self.MISSING),
                                 result(item) if kind == 'item.completed' else self.MISSING)]
        found = False
        for observation in observations:
            found = self._observe(*observation) or found
        return found
