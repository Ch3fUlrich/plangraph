"""Value-based child credential scrubbing, before any output truncation."""
import json
import re


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
    values = {v for k, v in env.items() if isinstance(v, str) and len(v) >= 6
              and (k in names or re.search(r'(?i)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)', k))}
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, '[autoos:redacted]')
    return text
