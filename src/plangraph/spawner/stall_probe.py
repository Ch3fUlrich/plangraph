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
        limit = float((record.get('card') or {}).get('stall_min', 12)) * 60
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
            if silent >= 2 * limit and not dry_run:
                from .runner import spawn_service
                result = asyncio.run(spawn_service(state=state).cancel(record['run_id']))
                event['cancelled'] = result.get('status') == 'cancelled'
            return event
        return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return event


def _record_pid_live(pid, start):
    """Tri-state existence: access/query failures never prove a dead process."""
    if os.name == 'nt':
        import ctypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = ctypes.c_uint32()
            if not k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return None
            if code.value != 259:
                return False
            from .win_recorded import creation
            token = creation(pid, handle)
            return None if token is None else start is None or token == start
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    import sys
    if sys.platform.startswith('linux'):
        from .proc_kill import _stat
        info = _stat(pid)
        return None if info is None else info[0] not in ('Z', 'X') and (start is None or info[4] == start)
    return True


def reconcile_record(record, directory, now=None):
    """Reconcile a read snapshot; watchdogs own persistence, status reads stay read-only."""
    import time
    if not isinstance(record, dict) or record.get('status') != 'running':
        return record
    now = time.time() if now is None else now
    try:
        record['live_log_age_s'] = max(0, now - (Path(directory) / 'live.log').stat().st_mtime)
    except OSError:
        pass
    pid = record.get('pid')
    if type(pid) is int and 0 < pid <= 0xffffffff and _record_pid_live(pid, record.get('pid_start')) is False:
        record.update(status='exited', reason='pid_gone', ended=now)
    return record
