"""Transport completion evidence; deliberately knows nothing about seat verdicts."""
import json

from .env_redact import normalize


def native_answer(job, stdout, stderr, exit_code):
    """Decode only agy's explicitly selected stream transport, failing closed."""
    if not (job.command and job.command[0] == 'agy' and '--input-format' in job.command
            and job.command[job.command.index('--input-format') + 1] == 'stream-json'):
        return stdout, stderr, exit_code
    result = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('event') == 'result':
            result = event.get('result')
    if not isinstance(result, dict):
        return '', stderr + '\nagy_stream_missing_result', exit_code or 1
    response = result.get('response')
    if result.get('status') != 'SUCCESS' or not isinstance(response, str):
        error = result.get('error') or 'invalid or unsuccessful result'
        return '', stderr + '\nagy_stream_error: ' + str(error), exit_code or 1
    return response, stderr, exit_code


def final_answer(stdout, *, allow_unfinished=False):
    """Keep the last stop-completed text, never a length/tool-call partial.

    A later empty step does not revoke an already completed answer. A later
    text part does: it must itself reach a stop before it can be published.
    Plain terminal output has no completion framing and is not evidence.
    Review verdicts may opt into pending text unless a finish reason other than
    stop/tool-calls (including a missing/unknown reason) blocks it. Only a
    text-bearing stop clears that block; empty stops and tool calls do not.
    """
    pending, complete = [], None
    truncated = False
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
                truncated = False
            elif part.get('reason') not in ('stop', 'tool-calls'):
                truncated = True
            pending = []
    return '\n'.join(pending) if allow_unfinished and pending and not truncated else complete


def answer_text(stdout):
    """Decode display text without treating unframed/unfinished text as completion."""
    complete = final_answer(stdout)
    if complete is not None:
        return complete
    texts = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            return stdout
        if not isinstance(event, dict):
            return stdout
        part = event.get('part')
        if event.get('type') == 'text' and isinstance(part, dict):
            text = part.get('text')
            if isinstance(text, str):
                texts.append(text)
    return '\n'.join(texts) if texts else stdout


def terminated_after_answer(job, stdout, stderr, exit_code):
    from .exit_outcome import is_opencode
    return (is_opencode(job) and exit_code == 1
            and any(normalize(line).strip() == 'Error: terminated'
                    for line in stderr.splitlines())
            and final_answer(stdout) is not None)
