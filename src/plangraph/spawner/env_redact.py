"""Value-based child credential scrubbing, before any output truncation."""
import json
import re

IDENTIFIER_KEYS = frozenset({'run_id', 'job_id', 'id', 'head_sha', 'branch', 'idempotency_key'})


def _is_numeric(value):
    """True for optionally signed decimal digits only: "-1", "+8080", "8080".

    Such values are configuration (sentinel, port, limit), never credential
    material, so a configured provider env name may expose them. Every other
    literal form stays redacted: "1_000" (digit grouping), "0x1F", "1.5" and
    friends can carry a secret payload.
    """
    return re.fullmatch(r'[+-]?\d+', value) is not None


def unavailable_reason(exit_code, stdout, stderr):
    """Failed harness evidence, in stderr-first order; never task prose on success."""
    if exit_code == 0:
        return None
    def normalize(text):
        return re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))', '', text).replace('\r\n', '\n').replace('\r', '\n')
    stdout, stderr = normalize(stdout), normalize(stderr)
    markers = ('model unavailable', 'not served', 'unknown model', 'not_served_on_host')
    def marked(value):
        return any(marker in value.lower() for marker in markers)

    def error_shaped(line):
        # D-3258: a marker is harness evidence only as a terminal reason or a
        # "marker: detail" diagnosis. Pytest prose carrying a marker (an assert
        # quoting one, a sentence mentioning one) is never a runtime denial.
        low = line.lower()
        for marker in markers:
            at = low.find(marker)
            while at != -1:
                tail = low[at + len(marker):].strip()
                if not tail or tail.startswith(':'):
                    return True
                at = low.find(marker, at + 1)
        return False

    def error_message(value):
        if isinstance(value, dict):
            return any((key == 'error' and marked(json.dumps(item))) or error_message(item)
                       for key, item in value.items())
        if isinstance(value, list):
            return any(error_message(item) for item in value)
        return False

    def evidence(line):
        text = line.strip()
        if error_shaped(line):
            return True
        if not marked(text):
            return False
        if text.lower().startswith('error:') or re.match(r'^HTTP \d{3}:', text):
            return True
        try:
            return error_message(json.loads(text))
        except ValueError:
            # A diagnostic may be one line of a pretty-printed JSON error.
            return bool(re.match(r'^[{\[,]?\s*"error"\s*:', text) and marked(text.split(':', 1)[1]))

    for line in stderr.splitlines():
        if evidence(line):
            return line
    for line in stdout.splitlines():
        if evidence(line):
            return line
    return None


def redact_env_values(text, env):
    names = set()
    try:
        providers = json.loads(env.get('OPENCODE_CONFIG_CONTENT', '{}')).get('providers', {})
        for provider in providers.values():
            if isinstance(provider, dict):
                # Same non-list-env rule as opencode_render: treat any non-list
                # `env` as absent instead of letting TypeError abort the sweep
                # and silently drop every collected name.
                env_list = provider.get('env')
                names.update(n for n in (env_list if isinstance(env_list, list) else ())
                             if isinstance(n, str))
        names.update(re.findall(r'\{env:([A-Za-z_][A-Za-z0-9_]*)\}', json.dumps(providers)))
    except (ValueError, TypeError, AttributeError):
        pass
    # Numeric carve-out: a configured provider env name (names) is redacted
    # unless its value is optionally signed decimal digits ("-1", "+8080");
    # "1_000" and every other non-decimal form stay redacted. Secret-shaped
    # key names are redacted regardless of the value's shape.
    values = {v for k, v in env.items() if isinstance(v, str) and v
              and ((k in names and not (_is_numeric(v) and not re.search(r'(?i)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|BEARER)', k))) or (len(v) >= 8 and not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', v)
                   and v.lower() not in {'true', 'false', 'yes', 'no', 'on', 'off'}
                   and re.search(r'(?i)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|BEARER)', k)))}
    values.update(line for value in tuple(values) if '\n' in value
                  for line in value.splitlines() if len(line) >= 8)
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, '[autoos:redacted]')
    return text
