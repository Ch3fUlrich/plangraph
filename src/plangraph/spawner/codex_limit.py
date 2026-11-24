"""Codex usage-limit replies as a quota line the leg-state store books with its reset time."""
import json
import re
import time
from datetime import datetime, timedelta

_LIMIT = re.compile(r"(?:error:\s*)?you've hit your usage limit\b", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}
_DATED = re.compile(r'try again at\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4}),?\s+'
                    r'(\d{1,2}):(\d{2})\s*([AP]M)', re.I)
_TIMED = re.compile(r'try again at\s+(\d{1,2}):(\d{2})\s*([AP]M)', re.I)


def _hour(hour, meridiem):
    return hour % 12 + (12 if meridiem.upper() == 'PM' else 0)


def reset_epoch(text, now):
    """The reset time a limit message spells, read in the host's local zone; None when absent."""
    try:
        if found := _DATED.search(text):
            month, day, year, hour, minute, meridiem = found.groups()
            return datetime(int(year), _MONTHS[month.lower()], int(day),
                            _hour(int(hour), meridiem), int(minute)).timestamp()
        if found := _TIMED.search(text):
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
        if _LIMIT.match(line):
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
