"""Host run evidence, bounded probe settings and the Codex usage-limit reply of a probe."""
import json
import math
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

def probe_setting(name, default, maximum):
    try:
        value = float(os.environ.get(name, default))
        return min(value, maximum) if math.isfinite(value) and value >= 0 else default
    except ValueError:
        return default

def probe_run(state, record, now, *, dry_run=False, hung=None):
    """Independent host sampling; missing evidence never authorizes cancellation."""
    if record.get('run_kind') in ('fleet-session', 'external-harness'):
        return None  # The external lifecycle journal owns these registrations.
    if record.get('run_kind') == 'native-codex' and record.get('overflow_remote'):
        return None  # Target-host watchdogs own remote liveness, even for older receipts.
    if hung is not None and hung.enabled:
        return hung.probe(state, record, now, dry_run=dry_run)
    import asyncio
    import math
    from subprocess import SubprocessError
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
                    try:
                        from .remote_ps import publish_run_record
                        publish_run_record(state, directory / 'record.json', json.dumps(record))
                    except (OSError, RuntimeError, SubprocessError):
                        event['record_error'] = 'could not publish orphaned run'
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

    def __init__(self, max_errors=None):
        from collections import deque
        self.seen, self.order, self.pending = set(), deque(), {}
        self.last, self.count = None, 0
        self.max_errors, self.errors, self.stop_reason = max_errors, 0, None

    @staticmethod
    def _polling(name, arguments):
        import shlex
        command = arguments.get('command', '') if isinstance(arguments, dict) else arguments
        if name.lower() not in ('bash', 'shell', 'command_execution', 'exec_command') or not isinstance(command, str):
            return False
        if any(char in command for char in ';|&><`$\n'):
            return False
        try:
            words = shlex.split(command)
        except ValueError:
            return False
        return (words[:2] in (['git', 'status'], ['git', 'diff'], ['git', 'log'])
                or words[:3] in (['gh', 'run', 'view'], ['gh', 'run', 'list'], ['gh', 'pr', 'checks']))

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
            call = (hashlib.sha256(json.dumps([name, arguments], sort_keys=True).encode()).digest(),
                    self._polling(name, arguments))
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
        if self.max_errors is not None:
            _, exit_code, is_error = result
            self.errors += is_error is True or (type(exit_code) is int and exit_code != 0)
            if self.errors >= self.max_errors:
                self.stop_reason = 'inline_tool_errors'
                return True
        call, polling = call
        if polling:
            self.last, self.count = None, 0
            return False
        digest = hashlib.sha256(call + json.dumps(result, sort_keys=True).encode()).digest()
        self.count = self.count + 1 if digest == self.last else 1
        self.last = digest
        if self.count >= self.REPEATS:
            self.stop_reason = 'tool_loop'
            return True
        return False

    def feed(self, line):
        try:
            frame = json.loads(line)
        except (ValueError, TypeError):
            return False
        if not isinstance(frame, dict):
            return False
        def result(part):
            if self.max_errors is not None and part.get('status') == 'error':
                return [part.get('error'), part.get('exit_code'), True]
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


_CODEX_LIMIT = re.compile(r"(?:error:\s*)?you've hit your usage limit\b", re.I)
_CODEX_MONTHS = {m: i for i, m in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}
_CODEX_DATED = re.compile(r'try again at\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4}),?\s+'
                    r'(\d{1,2}):(\d{2})\s*([AP]M)', re.I)
_CODEX_TIMED = re.compile(r'try again at\s+(\d{1,2}):(\d{2})\s*([AP]M)', re.I)


def _hour(hour, meridiem):
    return hour % 12 + (12 if meridiem.upper() == 'PM' else 0)


def reset_epoch(text, now):
    """The reset time a limit message spells, read in the host's local zone; None when absent."""
    try:
        if found := _CODEX_DATED.search(text):
            month, day, year, hour, minute, meridiem = found.groups()
            return datetime(int(year), _CODEX_MONTHS[month.lower()], int(day),
                            _hour(int(hour), meridiem), int(minute)).timestamp()
        if found := _CODEX_TIMED.search(text):
            hour, minute, meridiem = found.groups()
            at = datetime.fromtimestamp(now).replace(hour=_hour(int(hour), meridiem), minute=int(minute),
                                                     second=0, microsecond=0)
            return (at if at.timestamp() > now else at + timedelta(days=1)).timestamp()
    except (KeyError, ValueError, OverflowError, OSError):
        pass
    return None


def _messages(stream):
    """Complete error lines of one stream: plain lines and error events, never prose."""
    for line in (stream or '').replace('’', "'").splitlines():
        line = line.strip()
        if line.startswith('{'):
            try:
                event = json.loads(line)
                message = event.get('message') if event.get('type') == 'error' else (
                    (event.get('error') or {}).get('message') if event.get('type') == 'turn.failed' else None)
            except (ValueError, AttributeError):
                continue
            line = message.strip() if isinstance(message, str) else ''
        if _CODEX_LIMIT.match(line):
            yield line


def codex_usage_limit(exit_code, stdout, stderr, now=None):
    """A quota line for a failed Codex run that stopped on its usage limit, else None.

    The line carries "(reset after Ns)" when the message names a future reset time, so the store
    books the provider's own reset; a message without one books the store's fallback window.
    """
    if exit_code in (None, 0):
        return None
    now = time.time() if now is None else now
    for message in (*_messages(stderr), *_messages(stdout)):
        epoch = reset_epoch(message, now)
        wait = int(epoch - now) if epoch is not None else 0
        return f'HTTP 429: usage_limit (reset after {wait}s) {message}' if wait > 0 else f'HTTP 429: usage_limit {message}'
    return None
