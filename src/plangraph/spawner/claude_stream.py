"""Review text of a claude stream-json run (D-3605, D-3649).

``claude -p --output-format stream-json --verbose`` prints one JSON event per line.  A review seat's answer
-- the ``VERDICT:`` line ``tools/seat_verdict`` parses -- is the text of the final ``result`` event, so the
raw stream makes every Haiku seat a ``no_verdict``.  ``render_review_output`` therefore keeps the raw
(scrubbed) stream in ``claude-stream.jsonl`` next to the run's live.log and makes the final text the job's
stdout, i.e. the ``stdout_tail`` of the run record.  live.log still carries the raw stream as it arrives
(activity parsing comes later).
"""
from __future__ import annotations

import json
from pathlib import Path

from .output_keep import keep_output
from .private_io import atomic_write_private

RAW_NAME = 'claude-stream.jsonl'
#: Bytes of the raw stream kept per run; beyond that the head and the tail stay (see ``keep_output``).
RAW_KEEP = 8 * 1024 * 1024


def last_stop_reason(stream):
    """Last explicit result or assistant stop, even without usable text or a result."""
    stop = None
    for line in str(stream or '').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        source = event if event.get('type') == 'result' else event.get('message') if event.get('type') == 'assistant' else None
        if isinstance(source, dict) and isinstance(source.get('stop_reason'), str):
            stop = source['stop_reason']
        if event.get('type') == 'result' and event.get('safety_stops', 0) > 0: stop = 'refusal'
    return stop


def final_text(stream) -> tuple[str, str | None] | None:
    """``(text, stop_reason)`` of a finished stream-json run, or None when it holds no usable answer.

    The text is the last ``result`` event's ``result``; when there is none (a stream cut short, a CLI that
    prints no result event) the text blocks of the last assistant message.  An error result is no answer."""
    result = assistant = stop = None
    for line in str(stream or '').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'result':
            if event.get('is_error'):
                return None
            if isinstance(event.get('result'), str):
                result, stop = event['result'], event.get('stop_reason')
        elif event.get('type') == 'assistant':
            message = event.get('message')
            blocks = message.get('content') if isinstance(message, dict) else None
            texts = [b['text'] for b in blocks if isinstance(b, dict) and b.get('type') == 'text'
                     and isinstance(b.get('text'), str)] if isinstance(blocks, list) else []
            if texts:
                assistant = '\n'.join(texts)
    text = result if result is not None and result.strip() else assistant
    return (text, stop if isinstance(stop, str) else None) if text is not None and text.strip() else None


def render_review_output(job, stream, scrub) -> bool:
    """Make the final text of a finished run's stream the job's stdout (what the run record keeps as
    ``stdout_tail``) and keep the raw stream, scrubbed, in ``claude-stream.jsonl`` next to the run's live.log.

    ``job.stdout`` must still hold the scrubbed raw stream; ``scrub`` cleans the final text the same way.
    Nothing changes (False) when the stream has no usable answer or the raw stream cannot be kept (no run
    directory, write error): the raw stream is never dropped."""
    answer = final_text(stream)
    live_log = getattr(job, '_live_log', None)
    if answer is None or not live_log:
        return False
    try:
        atomic_write_private(Path(live_log).with_name(RAW_NAME), keep_output(job.stdout, RAW_KEEP))
    except OSError:
        job.events.append({'event': 'claude_stream_not_kept'})
        return False
    text, stop = answer
    job.stdout = scrub(text)
    job.events.append({'event': 'claude_stream_rendered', 'raw': RAW_NAME, 'stop_reason': stop})
    return True
