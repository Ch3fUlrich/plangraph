"""Transport completion evidence; deliberately knows nothing about seat verdicts."""
import json

from .env_redact import normalize


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
                if isinstance(error.get('message'), str):
                    from .env_redact import redact_env_values, redact_output
                    job.events.append(dict(event='opencode_error', message=redact_output(
                        redact_env_values(error['message'], env))[:1000]))
                output_limit |= (error.get('type') == 'output-limit' or error.get('code') == 'output-limit'
                    or 'Response still truncated after two output token limit continuations' in str(error.get('message', '')))
    output_limit |= any('output-limit' in normalize(line) or
        'Response still truncated after two output token limit continuations' in normalize(line)
        for line in stderr.splitlines())
    job.events.extend(steps)
    if output_limit:
        job.events.append(dict(event='opencode_output_limit'))
    # A zero-output length step followed by a real answer is already recovered.
    if not saw_text and not any((s['output_tokens'] or 0) > 0 for s in steps) and (
            steps and steps[-1]['finish_reason'] == 'length' and steps[-1]['output_tokens'] == 0
            or output_limit):
        # Probe the first child's isolated config on a copy; only the retry mutates its own env.
        job.events.append(dict(event='opencode_reasoning_exhausted',
                               cap_available=cap_reasoning(dict(env), job.model_or_combo, thinking_off=True)))


def cap_reasoning(env, model, *, refuse=False, thinking_off=False, reasoning_cap_tokens=None):
    """Validate an existing MiMo entry, then cap this run; optionally retain the seat retry control.

    Boolean probes never mutate on refusal. Launches request named SpawnRefused
    reasons; neither path invents providers/models or changes unrelated entries.
    """
    from .mimo_policy import ACCEPTED_MIMO_IDS
    from plangraph.catalog_data import SEED_MODELS
    from plangraph.routing_errors import SpawnRefused
    try:
        if model not in ACCEPTED_MIMO_IDS:
            raise SpawnRefused('reasoning_output_limit: unsupported_mimo_model')
        try:
            cfg = json.loads(env.get('OPENCODE_CONFIG_CONTENT'))
        except (ValueError, TypeError, RecursionError):
            raise SpawnRefused('reasoning_output_limit: malformed_config') from None
        if not isinstance(cfg, dict):
            raise SpawnRefused('reasoning_output_limit: malformed_config')
        provider, key = model.split('/', 1)
        node = cfg
        for name, label in (('providers', 'providers'), (provider, 'provider'),
                            ('models', 'models'), (key, 'model')):
            if name not in node:
                raise SpawnRefused('reasoning_output_limit: missing_' + label)
            node = node[name]
            if not isinstance(node, dict):
                raise SpawnRefused('reasoning_output_limit: malformed_' + label)
        block, entry = cfg['providers'][provider], node
        if entry.get('package', block.get('package')) != '@opencode/ai/providers/openai-compatible':
            raise SpawnRefused('reasoning_output_limit: non_openai_compatible')
        for owner in (block, entry):
            for field in ('limit', 'body'):
                if not isinstance(owner.get(field, {}), dict):
                    raise SpawnRefused('reasoning_output_limit: malformed_' + field)
        maximum = next((row.get('max_output_tokens') for row in SEED_MODELS
                        if row.get('model_id') == model), None)
        if type(maximum) is not int or maximum <= 0:
            raise SpawnRefused('reasoning_output_limit: invalid_catalog_output')
        if thinking_off and entry.get('body', {}).get('thinking') == {'type': 'disabled'}:
            return False
        cap = (maximum // 2 if reasoning_cap_tokens is True else
               min(reasoning_cap_tokens, maximum - 1) if type(reasoning_cap_tokens) is int and reasoning_cap_tokens > 0 and maximum > 1 else
               None)
        if cap is not None and not thinking_off and entry.get('body', {}).get('thinking') != {'type': 'disabled'}:
            body = entry.setdefault('body', {})
            reasoning = body.get('reasoning')
            if reasoning is None:
                body['reasoning'] = {'max_tokens': cap}
            elif not isinstance(reasoning, dict):
                raise SpawnRefused('reasoning_output_limit: malformed_reasoning')
            else:
                current = reasoning.get('max_tokens')
                if type(current) is not int or current <= 0 or current >= maximum:
                    reasoning['max_tokens'] = cap
        entry.setdefault('limit', {})['output'] = maximum
        if thinking_off:
            entry.setdefault('body', {})['thinking'] = {'type': 'disabled'}
        # Older harnesses also clamp model.limit.output by a process-wide default.
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps(cfg, sort_keys=True)
        env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'] = str(maximum)
        return True
    except SpawnRefused:
        if refuse:
            raise
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
