"""Per-run stderr.log: diagnostics kept apart from the repeatedly shortened JSON tail."""
from .private_io import atomic_write_private

MAX_BYTES = 1024 * 1024
MARKER = b'\n[stderr truncated]\n'


def write_stderr_log(captures, jobs, record, path, clean):
    """Merge this save's stderr into `captures` (job id -> text), then write a capped private stderr.log."""
    job = jobs.get(record.get('job_id'))
    if job is not None:
        captures[record['job_id']] = clean(getattr(job, 'stderr', '') or '')
    else:
        text = clean(record.get('stderr_tail', ''))
        previous = captures.get('record', '')
        if not previous.endswith(text):
            captures['record'] = previous + text
        elif not captures:
            captures['record'] = text
    data = ''.join(captures.values()).encode('utf-8')
    if len(data) > MAX_BYTES:
        data = data[:MAX_BYTES - len(MARKER)].decode('utf-8', errors='ignore').encode() + MARKER
    atomic_write_private(path, data.decode('utf-8'))


def clean_attempts(record, clean):
    """Redact attempt diagnostics: skip_reason without ANSI (<=300 chars), stderr_tail capped at 4 KiB."""
    from .env_redact import normalize
    for attempt in record.get('attempts', []):
        if attempt.get('skip_reason'):
            attempt['skip_reason'] = normalize(clean(attempt['skip_reason']))[:300]
        if 'stderr_tail' in attempt:
            attempt['stderr_tail'] = clean(attempt['stderr_tail']).encode()[-4096:].decode('utf-8', errors='ignore')
