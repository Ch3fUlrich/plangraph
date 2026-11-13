"""Transport completion evidence; deliberately knows nothing about seat verdicts."""
import json

from .env_redact import normalize


def reasoning_only_length(step):
    return (step.get('finish_reason') == 'length'
            and (step.get('reasoning_tokens') or 0) > 0)


class ReasoningLengthGate:
    """Wake collection on a reasoning-only length frame, even with open pipes."""
    def __init__(self, job, stopped):
        from .exit_outcome import is_opencode
        self.active = is_opencode(job) and job.role == 'review'
        self.stopped, self.exhausted = stopped, False
        self.pending, self.saw_text = '', False

    def feed(self, text):
        if not self.active:
            return
        lines = (self.pending + text).split('\n')
        self.pending = lines.pop()
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                self.saw_text |= bool(line.strip())
                continue
            if not isinstance(event, dict) or not isinstance(event.get('part'), dict):
                continue
            part = event['part']
            if event.get('type') == 'text':
                self.saw_text |= isinstance(part.get('text'), str) and bool(part['text'].strip())
            tokens = part.get('tokens') or {}
            reasoning = tokens.get('reasoning') if isinstance(tokens, dict) else None
            if event.get('type') == 'step_finish' and part.get('reason') == 'length' and type(reasoning) is int and reasoning > 0:
                self.exhausted = not self.saw_text
        if self.saw_text:
            self.exhausted = False
        if self.exhausted:
            self.stopped.set()


def retry_prompt(prompt):
    """The original evidence already lives in the continued session."""
    return 'answer now: findings, then the VERDICT line; no further analysis'


class ReviewSession:
    """Retain one isolated store across continuations, including cancellation."""
    def __init__(self, deadline=None):
        self.directory, self.id, self.closed = None, None, False
        self.deadline = deadline
        self.jobs = set()

    def release(self, job_id=None):
        import shutil
        self.jobs.discard(job_id)
        if self.closed and not self.jobs and self.directory:
            shutil.rmtree(self.directory, ignore_errors=True)

    def close(self):
        self.closed = True
        self.release()

    def isolate(self, env, job_id):
        from pathlib import Path
        from .opencode_isolation import close_config_layers, CLOSED_LAYERS
        self.directory = self.directory or close_config_layers(env)
        env.update(CLOSED_LAYERS, OPENCODE_CONFIG_DIR=self.directory,
                   OPENCODE_CONFIG=str(Path(self.directory) / 'pg-empty.json'))
        self.jobs.add(job_id)
        return self.directory


def record_steps(job, stdout, stderr, env):
    """Retain diagnostic step usage before stdout becomes display text, not a witness."""
    from .exit_outcome import is_opencode
    if not is_opencode(job) or job.role != 'review':
        return
    steps, output_limit = [], False
    saw_text = bool(answer_text(stdout, raw_fallback=False).strip())
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get('sessionID'), str) and event['sessionID'].startswith('ses_'):
            job.session_id = event['sessionID']
        part = event.get('part')
        if event.get('type') == 'step_finish' and isinstance(part, dict):
            tokens = part.get('tokens')
            tokens = tokens if isinstance(tokens, dict) else {}
            usage = {key + '_tokens': value if type(value) is int and value >= 0 else None
                     for key in ('output', 'reasoning') for value in [tokens.get(key)]}
            steps.append(dict(event='opencode_step_finish', finish_reason=part.get('reason'), **usage))
        elif event.get('type') == 'error':
            error = event.get('error')
            if isinstance(error, dict):
                output_limit |= (error.get('type') == 'output-limit' or error.get('code') == 'output-limit'
                    or 'Response still truncated after two output token limit continuations' in str(error.get('message', '')))
    output_limit |= any('output-limit' in normalize(line) or
        'Response still truncated after two output token limit continuations' in normalize(line)
        for line in stderr.splitlines())
    job.events.extend(steps)
    if output_limit:
        job.events.append(dict(event='opencode_output_limit'))
    length = bool(steps and reasoning_only_length(steps[-1]))
    # Text, not an output-token counter (which can be 1 with no text), owns recovery.
    if not saw_text and (length or not any((s['output_tokens'] or 0) > 0 for s in steps) and (
            steps and steps[-1]['finish_reason'] == 'length' and steps[-1]['output_tokens'] == 0
            or output_limit)):
        job.events.append(dict(event='opencode_reasoning_exhausted',
                                **({'reason': 'reasoning_only_length'} if length else {})))


def cap_reasoning(env, model):
    """Set MiMo's combined reasoning/answer output cap from the model catalog."""
    from .mimo_policy import ACCEPTED_MIMO_IDS
    from plangraph.catalog_data import SEED_MODELS
    if model not in ACCEPTED_MIMO_IDS:
        return False
    try:
        cfg = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        provider, key = model.split('/', 1)
        block = cfg.setdefault('providers', cfg.get('provider', {})).setdefault(provider, {})
        entry = block.setdefault('models', {}).setdefault(key, {})
        if not isinstance(entry, dict) or not isinstance(entry.get('body', {}), dict):
            return False
        maximum = next(row['max_output_tokens'] for row in SEED_MODELS if row['model_id'] == model)
        entry.setdefault('limit', {})['output'] = maximum
        # Older harnesses also clamp model.limit.output by this process-wide default (32000).
        env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'] = str(maximum)
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps(cfg, sort_keys=True)
        return True
    except (KeyError, ValueError, TypeError, AttributeError, StopIteration):
        return False


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
    non-blank text-bearing stop clears that block; empty/whitespace-only stops
    and tool calls do not.
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
            if part.get('reason') == 'stop' and any(p.strip() for p in pending):
                complete = '\n'.join(pending)
                truncated = False
            elif part.get('reason') not in ('stop', 'tool-calls'):
                truncated = True
            pending = []
    return '\n'.join(pending) if allow_unfinished and pending and not truncated else complete


def answer_text(stdout, *, raw_fallback=True):
    """Decode display text; raw_fallback=False excludes diagnostic-only JSON, not plain text."""
    complete = final_answer(stdout)
    if complete is not None:
        return complete
    texts = []
    for line in stdout.splitlines():
        if not raw_fallback and not line.strip():
            continue
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
    return '\n'.join(texts) if texts else stdout if raw_fallback else ''


def terminated_after_answer(job, stdout, stderr, exit_code):
    from .exit_outcome import is_opencode
    return (is_opencode(job) and exit_code == 1
            and any(normalize(line).strip() == 'Error: terminated'
                    for line in stderr.splitlines())
            and final_answer(stdout) is not None)
