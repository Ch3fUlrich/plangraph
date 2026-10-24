"""Value-based child credential scrubbing, before any output truncation."""
import json
import re


def unavailable_reason(exit_code, stdout, stderr):
    """Failed harness evidence, in stderr-first order; never task prose on success."""
    if exit_code == 0:
        return None
    markers = ('model unavailable', 'not served', 'unknown model', 'not_served_on_host')
    return next((line for stream in (stderr, stdout) for line in stream.splitlines()
                 if any(marker in line.lower() for marker in markers)), None)


def redact_env_values(text, env):
    names = set()
    try:
        providers = json.loads(env.get('OPENCODE_CONFIG_CONTENT', '{}')).get('providers', {})
        for provider in providers.values():
            if isinstance(provider, dict):
                names.update(n for n in provider.get('env', []) if isinstance(n, str))
        names.update(re.findall(r'\{env:([A-Za-z_][A-Za-z0-9_]*)\}', json.dumps(providers)))
    except (ValueError, TypeError, AttributeError):
        pass
    values = {v for k, v in env.items() if isinstance(v, str) and len(v) >= 4
              and (k in names or re.search(r'(?i)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|BEARER)', k))}
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, '[autoos:redacted]')
    return text
