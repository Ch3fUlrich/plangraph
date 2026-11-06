"""Transport completion evidence; deliberately knows nothing about seat verdicts."""
import json

from .env_redact import normalize


def final_answer(stdout):
    """Keep the last stop-completed text, never a length/tool-call partial.

    A later empty step does not revoke an already completed answer. A later
    text part does: it must itself reach a stop before it can be published.
    Plain terminal output has no completion framing and is not evidence.
    """
    pending, complete = [], None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        part = event.get('part')
        if not isinstance(part, dict):
            continue
        if event.get('type') == 'text':
            text = part.get('text')
            if isinstance(text, str) and text.strip():
                pending.append(text)
                complete = None
        elif event.get('type') == 'step_finish':
            if part.get('reason') == 'stop' and pending:
                complete = '\n'.join(pending)
            pending = []
    return complete


def terminated_after_answer(job, stdout, stderr, exit_code):
    from .exit_outcome import is_opencode
    return (is_opencode(job) and exit_code == 1
            and any(normalize(line).strip() == 'Error: terminated'
                    for line in stderr.splitlines())
            and final_answer(stdout) is not None)
