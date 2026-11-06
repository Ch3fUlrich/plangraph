"""Value-based child credential scrubbing, before any output truncation."""
import json
import re


def redact(value):
    """Shared fail-closed output scrubber; never return raw text on failure."""
    try:
        if isinstance(value, dict):
            return {k: ('[REDACTED]' if k != 'idempotency_key' and re.search(
                r'(?i)(key|authorization|token|secret)', k) else redact(v))
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact(v) for v in value]
        if isinstance(value, str):
            value = re.sub(r'\b(?:x?sk)-[A-Za-z0-9_-]+', '[REDACTED]', value)
            value = re.sub(r'(?i)Bearer\s+[^\s"\x27]+', 'Bearer [REDACTED]', value)
            # Greedily consume through the last @ within the quoted/space boundary.
            value = re.sub(r'(\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:\x22\x27]+:)[^\s\x22\x27]*(@)',
                           r'\1[REDACTED]\2', value)
            return re.sub(
                r'(?im)((?:["\x27])?\b(?:\w*_KEY|PASSWORD|PG_DSN|TOKEN|SECRET)'
                r'(?:["\x27])?\s*[=:]\s*)(?:"(?:\\.|[^"\\\r\n])*"'
                r"|'(?:\\.|[^'\\\r\n])*'|[^\r\n]+)",
                lambda m: m[1] + (m[0][len(m[1])] + '[REDACTED]' + m[0][-1]
                                  if m[0][len(m[1])] in '\"\x27' and
                                  m[0][-1] == m[0][len(m[1])] else '[REDACTED]'), value)
        return value
    except Exception:
        return '[autoos:redaction failed; output withheld]\n'


class OutputLines:
    """Hold incomplete lines across reads, including cancellation/EOF tails."""
    def __init__(self, scrub):
        self.scrub, self.pending, self.discarding = scrub, '', False

    def feed(self, text, final=False):
        self.pending += text
        if self.discarding:
            match = re.search(r'[\r\n]', self.pending)
            self.pending = self.pending[match.end():] if match else ''
            self.discarding = not bool(match)
        end = len(self.pending) if final else max(self.pending.rfind('\n'), self.pending.rfind('\r')) + 1
        text, self.pending = self.pending[:end], self.pending[end:]
        if len(self.pending) > 65536:
            # An arbitrarily long credential cannot safely be partially emitted.
            text += '[autoos:overlong line withheld]\n'
            self.pending, self.discarding = '', True
        if not text:
            return ''
        try:
            return self.scrub(text)
        except Exception:
            return '[autoos:redaction failed; output withheld]\n'

IDENTIFIER_KEYS = frozenset({'run_id', 'job_id', 'id', 'head_sha', 'branch', 'idempotency_key'})

#: Credit/auth refusals (D-3605 a): the leg is as unusable as a rate-limited one.
#: No bare HTTP 401/402/403 alternative: a run that merely prints those codes
#: in prose ("HTTP 403: forbidden (expected)") is healthy, not a refusal (F1).
CREDIT_AUTH_MARKER = re.compile(
    r'credits? (?:exhausted|depleted|exceeded|ran out)|out of credits|insufficient (?:credits?|balance|quota)'
    r'|payment required|unauthori[sz]ed|authentication (?:failed|error|required)|invalid api key', re.I)
_QUOTA_MARKER = re.compile(
    r'quota reached|quota exceeded|quota threshold|exhausted their quota|rate limit|resets in'
    r'|too many requests|\bHTTP 429\b|' + CREDIT_AUTH_MARKER.pattern, re.I)
#: D-3685: OmniRoute appends "(reset after N)" to its quota line. The suffix is
#: not quota evidence on its own: a line merely starting with it never marks a
#: leg (prose stays out, D-3257). It counts only on an error-shaped line or
#: next to a quota/rate-limit name.
_RESET_AFTER = re.compile(r'\(reset after \d', re.I)
_ERROR_SHAPED = re.compile(r'\s*(?:error:|HTTP \d{3}:)', re.I)
_USAGE_LIMIT = re.compile(
    r'\s*(?:(?:error:|HTTP \d{3}:)\s*)?(?:free |go )?usage limit '
    r'(?:reached|exceeded)(?:\s*[.!]?(?:\s*\(reset after \d[^\n)]*\))?'
    r'|\.\s*Please try again later[.!]?|,\s*retry in \d+(?:\.\d+)?\s*(?:s|m|h|d))\s*', re.I)
_CREDIT_LIMIT = re.compile(
    r"\s*You've reached your credit usage limit\. Please upgrade your subscription plan "
    r'to get more resources\. Report Issue \(input /feedback\)\s*', re.I)
_MODEL_UNAVAILABLE = re.compile(
    r'\s*(?:(?:error:|HTTP \d{3}:)\s*)?Model unavailable:\s*\S+\s*', re.I)
_BUSY_LINE = re.compile(
    r'\s*(?:error:\s*(?:server busy|too busy|overloaded)|HTTP 503: Service Unavailable)\s*$', re.I)


def busy_refusal(text):
    """Unavailable dispatch evidence, not a mid-sentence discussion of errors."""
    for line in normalize(text).splitlines():
        if _BUSY_LINE.match(line):
            return line.strip()
        if not line.strip().startswith('{'):
            continue
        try:
            response = json.loads(line).get('autoos_provider_response', {})
            if response.get('status') == 503:
                return 'HTTP 503: ' + ' '.join(normalize(str(response.get('body', ''))).split())[:200]
        except (ValueError, TypeError, AttributeError):
            pass
    return None


def _is_numeric(value):
    """True for optionally signed decimal digits only: "-1", "+8080", "8080".

    Such values are configuration (sentinel, port, limit), never credential
    material, so a configured provider env name may expose them. Every other
    literal form stays redacted: "1_000" (digit grouping), "0x1F", "1.5" and
    friends can carry a secret payload.
    """
    return re.fullmatch(r'[+-]?\d+', value) is not None


def normalize(text):
    return re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))', '', text).replace('\r\n', '\n').replace('\r', '\n')


def first_reason(text):
    return next((line.strip() for line in normalize(text).splitlines() if line.strip()), '')


def harness_limit_line(exit_code, stdout, stderr):
    if exit_code in (None, 0):
        return None
    busy = busy_refusal(stderr) or busy_refusal(stdout)
    if busy:
        return busy
    # Quota evidence needs an error-shaped or start-anchored line (D-3257):
    # a mid-line mention in prose, a traceback or a retry log never marks a
    # leg. Agent prose on stdout only counts via its error-shaped lines.
    def quota(line):
        if _CREDIT_LIMIT.fullmatch(re.sub(r'^\s*error:\s*', '', line, flags=re.I)):
            return True
        if re.search(r'usage limit', line, re.I) and not _QUOTA_MARKER.search(line):
            return _USAGE_LIMIT.fullmatch(line)
        if _ERROR_SHAPED.match(line):
            return _QUOTA_MARKER.search(line) or _RESET_AFTER.search(line)
        # Start-anchored only (D-3257): the reset-after suffix alone never
        # marks; it needs a quota/rate-limit name alongside.
        return _QUOTA_MARKER.match(line.strip()) or (
            _RESET_AFTER.match(line.strip()) and _QUOTA_MARKER.search(line))
    lines = [line for line in normalize(stderr).splitlines() if quota(line)] + [
        line for line in normalize(stdout).splitlines()
        if _ERROR_SHAPED.match(line) and quota(line)]
    return lines[0].strip() if lines else None


def early_refusal(complete_text):
    """First refusal line (rate limit, credits, auth, 429/402 envelope) in complete output lines.

    Streamed while the child still runs, so a refused dispatch ends at once
    instead of showing 'running' until a watchdog fires (D-3605 a).  Both
    streams need an error-shaped line ('Error:' / 'HTTP nnn:' prefix) plus a
    quota marker: a start-anchored retry log ('Rate limit reached, retrying')
    or marker prose under an error shape never kills a healthy run (F1).
    """
    busy = busy_refusal(complete_text)
    if busy:
        return busy
    lines = normalize(complete_text).splitlines()
    for line in lines:
        if re.search(r'usage limit', line, re.I) and not _QUOTA_MARKER.search(line):
            if _ERROR_SHAPED.match(line) and _USAGE_LIMIT.fullmatch(line):
                return line.strip()
            continue
        text = line.strip()
        if text.startswith('{"autoos_provider_response"'):
            try:
                response = json.loads(text)['autoos_provider_response']
                if response.get('status') in (429, 402):
                    return 'HTTP %s: %s' % (response['status'], str(response.get('body', ''))[:200])
            except (ValueError, TypeError, AttributeError, KeyError):
                pass
    for line in lines:
        if _ERROR_SHAPED.match(line) and (_QUOTA_MARKER.search(line) or _RESET_AFTER.search(line)):
            return line.strip()
    return None


def unavailable_reason(exit_code, stdout, stderr):
    """Failed harness evidence, in stderr-first order; never task prose on success."""
    if exit_code == 0:
        return None
    busy = busy_refusal(stderr) or busy_refusal(stdout)
    if busy:
        return busy
    stdout, stderr = normalize(stdout), normalize(stderr)
    markers = ('model unavailable', 'not served', 'unknown model', 'not_served_on_host')
    def marked(value):
        return any(marker in value.lower() for marker in markers)

    def error_message(value):
        if isinstance(value, dict):
            return any((key == 'error' and marked(json.dumps(item))) or error_message(item)
                       for key, item in value.items())
        if isinstance(value, list):
            return any(error_message(item) for item in value)
        return False

    for is_stderr, line in [(True, line) for line in stderr.splitlines()] + [(False, line) for line in stdout.splitlines()]:
        text = line.strip()
        if (_MODEL_UNAVAILABLE.fullmatch(text)
                or (is_stderr and re.fullmatch(r'not_served_on_host:\s*\S+(?:\s+\S+)*', text))):
            return text
        if 'model unavailable' in text.lower() and not text.startswith('{'):
            if not _ERROR_SHAPED.match(text):
                continue
        if not marked(text):
            continue
        if text.lower().startswith('error:') or re.match(r'^HTTP \d{3}:', text):
            return line
        try:
            if error_message(json.loads(text)):
                return line
        except ValueError:
            # A diagnostic may be one line of a pretty-printed JSON error.
            if re.match(r'^[{\[,]?\s*"error"\s*:', text) and marked(text.split(':', 1)[1]):
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
