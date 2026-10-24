"""Deterministic fleet-bus/2 primitives; callers supply time, keys and state."""

import hashlib
import hmac
import json
import math
import re
from collections import OrderedDict, deque
from dataclasses import dataclass, field

VERSION = "fleet-bus/2"
BODY_MAX = 4096
TEXT_MAX = 2000
SKEW_S = 60
NONCE_CAP = 10000
RING_CAP = 1000
RATE_PER_MIN = 30

ERROR_STATUS = {
    "bad_request": 400,
    "skew": 401,
    "unknown_kid": 401,
    "bad_signature": 401,
    "replayed_nonce": 401,
    "not_found": 404,
    "too_large": 413,
    "cursor_gone": 410,
    "rate_limited": 429,
}


class BusError(Exception):
    """A public rejection with no request or key material attached."""

    def __init__(self, code, message="Request rejected", retry_after_s=None):
        super().__init__(message)
        self.code = code
        self.status = ERROR_STATUS[code]
        self.message = message
        self.retry_after_s = retry_after_s


def error_body(error):
    """Return the language-neutral error response."""
    return {"error": {"code": error.code, "message": error.message}}


def scrub_text(value):
    """Flatten controls and whitespace, then cap display text by code points."""
    flattened = "".join(
        " " if ord(char) < 32 or 127 <= ord(char) <= 159
        or 0xD800 <= ord(char) <= 0xDFFF else char
        for char in value
    )
    return " ".join(flattened.split())[:TEXT_MAX]


def parse_ts(value):
    """Parse nonnegative integral epoch seconds, never booleans or fractions."""
    if isinstance(value, bool):
        raise BusError("bad_request")
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9]+", value):
            raise BusError("bad_request")
        try:
            return int(value)
        except ValueError:
            raise BusError("bad_request") from None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    raise BusError("bad_request")


def canonical_post(envelope):
    """Serialize the signed POST fields, hashing original unsanitized text."""
    return "|".join([
        VERSION, "post", str(parse_ts(envelope["ts"])), envelope["nonce"],
        envelope["from"], envelope["to"], envelope["msg_id"],
        envelope.get("kid", ""),
        hashlib.sha256(envelope["text"].encode("utf-8")).hexdigest(),
    ])


def canonical_read(ts, nonce, reader, kid=None, cursor=""):
    """Serialize a READ credential bound to its delivery cursor."""
    return "|".join([VERSION, "read", str(parse_ts(ts)), nonce, reader, kid or "", cursor])


def sign(secret, canonical):
    """Return lowercase hexadecimal HMAC-SHA256 over UTF-8 canonical bytes."""
    return hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


class RateLimiter:
    """Allow thirty authenticated requests per sender in a rolling minute."""

    def __init__(self):
        self.senders = {}

    def check(self, sender, now):
        # Retire idle senders so identities do not accumulate indefinitely.
        for identity, times in list(self.senders.items()):
            while times and times[0] <= now - 60:
                times.popleft()
            if not times:
                del self.senders[identity]
        times = self.senders.setdefault(sender, deque())
        if len(times) >= RATE_PER_MIN:
            raise BusError("rate_limited", retry_after_s=max(1, math.ceil(times[0] + 60 - now)))
        times.append(now)


@dataclass
class BusState:
    """In-memory contract state; transport adapters must serialize access."""

    seen: OrderedDict = field(default_factory=OrderedDict)
    msg_ids: dict = field(default_factory=dict)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    ring: deque = field(default_factory=deque)
    last_ms: int = -1
    sequence: int = 0


def new_state():
    """Create independent replay, dedupe, delivery and rate state."""
    return BusState()


def _identifier(value, cap):
    # Delimiters and controls would make pipe-separated signing ambiguous.
    if not isinstance(value, str) or not 1 <= len(value) <= cap:
        raise BusError("bad_request")
    if any(char == "|" or char.isspace() or ord(char) < 33 or 127 <= ord(char) <= 159
           or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise BusError("bad_request")


def _shape(envelope):
    required = {"from", "to", "ts", "nonce", "msg_id", "text"}
    optional = {"trace_id", "key", "run_id", "attempt_n", "kid"}
    if not isinstance(envelope, dict) or not required <= envelope.keys():
        raise BusError("bad_request")
    if envelope.keys() - required - optional:
        raise BusError("bad_request")
    for name in (required | optional) - {"ts", "text", "attempt_n"}:
        if name in envelope:
            _identifier(envelope[name], 128 if name == "nonce" else 64)
    parse_ts(envelope["ts"])
    if not isinstance(envelope["text"], str):
        raise BusError("bad_request")
    try:
        envelope["text"].encode("utf-8")
    except UnicodeEncodeError:
        raise BusError("bad_request") from None
    if "attempt_n" in envelope:
        attempt = envelope["attempt_n"]
        if type(attempt) is not int or not 1 <= attempt <= 1000:
            raise BusError("bad_request")


def _authenticate(resolve_key, ts, nonce, sender, kid, canonical, signature, now, state):
    if abs(now - parse_ts(ts)) > SKEW_S:
        raise BusError("skew")
    secret = resolve_key(kid)
    if secret is None:
        raise BusError("unknown_kid")
    expected = sign(secret, canonical)
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise BusError("bad_signature")
    if not hmac.compare_digest(expected, signature):
        raise BusError("bad_signature")
    if nonce in state.seen:
        raise BusError("replayed_nonce")
    # Only authenticated requests may consume replay or rate state.
    state.seen[nonce] = now
    while len(state.seen) > NONCE_CAP:
        state.seen.popitem(last=False)
    state.limiter.check(sender, now)


def validate_post(resolve_key, body, signature, now, state):
    """Validate a wire POST and return its original or newly assigned ack."""
    if not isinstance(body, (bytes, bytearray)):
        raise BusError("bad_request")
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        # Malformed oversized input has no shape to validate first.
        raise BusError("too_large" if len(body) > BODY_MAX else "bad_request") from None
    _shape(envelope)
    if len(body) > BODY_MAX:
        raise BusError("too_large")
    _authenticate(resolve_key, envelope["ts"], envelope["nonce"], envelope["from"],
                  envelope.get("kid"), canonical_post(envelope), signature, now, state)
    key = (envelope["from"], envelope["msg_id"])
    if key in state.msg_ids:
        return dict(state.msg_ids[key], dup=True)
    milliseconds = max(int(now * 1000), state.last_ms)
    sequence = state.sequence + 1 if milliseconds == state.last_ms else 1
    if sequence > 999999:
        milliseconds += 1
        sequence = 1
    state.last_ms, state.sequence = milliseconds, sequence
    ack = {"id": f"{milliseconds}-{sequence:06d}", "ts": now,
           "msg_id": envelope["msg_id"], "dup": False}
    record = {name: envelope[name] for name in
              ("from", "to", "msg_id", "text", "trace_id", "key", "run_id", "attempt_n")
              if name in envelope}
    record.update(id=ack["id"], ts=ack["ts"], text=scrub_text(envelope["text"]))
    if len(state.ring) == RING_CAP:
        evicted = state.ring.popleft()
        state.msg_ids.pop((evicted["from"], evicted["msg_id"]), None)
    state.ring.append(record)
    state.msg_ids[key] = ack.copy()
    return ack


def check_cursor(cursor, ring_ids):
    """Return the next index, refusing any nonempty cursor not retained."""
    if cursor == "":
        return 0
    if not isinstance(cursor, str) or not re.fullmatch(r"[0-9]+-[0-9]{6}", cursor):
        raise BusError("cursor_gone")
    try:
        return list(ring_ids).index(cursor) + 1
    except ValueError:
        raise BusError("cursor_gone") from None


def parse_limit(value):
    """Accept integer page sizes 1..200, defaulting to 100."""
    if value is None:
        return 100
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= 200:
        raise BusError("bad_request")
    return value


def check_read(resolve_key, headers, cursor, now, state):
    """Authenticate READ before calling read_records; no unauthenticated GET."""
    headers = {name.lower(): value for name, value in headers.items()}
    signature = headers.get("x-fleet-sig")
    if not signature:
        raise BusError("bad_signature")
    reader = headers.get("x-fleet-from")
    nonce = headers.get("x-fleet-nonce")
    kid = headers.get("x-fleet-kid")
    ts = parse_ts(headers.get("x-fleet-ts"))
    _identifier(reader, 64)
    _identifier(nonce, 128)
    if kid is not None:
        _identifier(kid, 64)
    if not isinstance(cursor, str) or "|" in cursor or any(
            0xD800 <= ord(char) <= 0xDFFF for char in cursor):
        raise BusError("bad_request")
    _authenticate(resolve_key, ts, nonce, reader, kid,
                  canonical_read(ts, nonce, reader, kid, cursor), signature, now, state)
    return reader


def read_records(state, cursor="", limit=100):
    """Shape a page after authentication; cursor tracks the last delivered row."""
    limit = parse_limit(limit)
    ring = list(state.ring)
    start = check_cursor(cursor, [record["id"] for record in ring])
    records = [record.copy() for record in ring[start:start + limit]]
    return {"cursor": records[-1]["id"] if records else cursor, "records": records}
