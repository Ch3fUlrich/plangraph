"""Stream and structure secret scrubbing for spawner runners (Part A)."""
from __future__ import annotations

import re

_KEY_RE = re.compile(r'(?i)(PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|DSN|CREDENTIAL|AUTH|BEARER)')
_URL_RE = re.compile(r'([a-zA-Z][a-zA-Z0-9+.-]*://[^:\s/@]*:)([^@\s]+)(@)')
_JSON_KV = re.compile(r'("([^"]+)"\s*:\s*)("(?:[^"\\]|\\.)*"|[^\s,}\]]+)')
_EQ_KV = re.compile(r'(?i)\b([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*("[^"]*"|\'[^\']*\'|[^\s\x00\r\n;]+)')
_COLON_KV = re.compile(r'(?m)^(\s*(?:-[ \t]+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*:\s+)([^\r\n]+)')


def _wrap(pre: str, val: str) -> str:
    if val.startswith('"') and val.endswith('"') and len(val) >= 2:
        return f'{pre}"[autoos:redacted]"'
    if val.startswith("'") and val.endswith("'") and len(val) >= 2:
        return f"{pre}'[autoos:redacted]'"
    return f'{pre}[autoos:redacted]'


def scrub_secrets(text: str) -> str:
    """Scrub credential URLs and key-value secret lines/pairs."""
    if not isinstance(text, str) or not text:
        return text
    text = _URL_RE.sub(r'\g<1>[autoos:redacted]\3', text)
    text = _JSON_KV.sub(lambda m: _wrap(m.group(1), m.group(3)) if _KEY_RE.search(m.group(2)) else m.group(0), text)
    text = _EQ_KV.sub(lambda m: _wrap(f'{m.group(1)}=', m.group(2)) if _KEY_RE.search(m.group(1)) else m.group(0), text)
    return _COLON_KV.sub(lambda m: _wrap(m.group(1), m.group(3).strip()) if _KEY_RE.search(m.group(2)) else m.group(0), text)


def redact(value):
    """Scrub credential-shaped strings recursively before persistence or display."""
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if k != 'idempotency_key' and re.search(r'(?i)(key|authorization|token|secret)', k)
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = scrub_secrets(value)
        value = re.sub(r'sk-[\w-]+', '[REDACTED]', value)
        value = re.sub(r'(?i)Bearer\s+(?!\[(?:autoos:)?redacted\])[^\s"\x27]+', 'Bearer [REDACTED]', value)
        return re.sub(r'(?i)(\b\w*_KEY=)(?!\[(?:autoos:)?redacted\])[^\s"\x27]+', r'\1[REDACTED]', value)
    return value
