"""PG-ANSWER-UI: the operator HTTP hop, kept out of ``api.py`` (size budget).

The operator principal is an ordinary stored principal whose bearer is the
*proxy credential* — held only by the nginx sidecar, never by a browser.  On
top of that bearer every request names the Authelia user in
``X-PG-Operator-User``, and that name must be in the config allow-list.  A
header any direct client can send must never grant anything, so:

* a bearer that is not the operator is refused as soon as the header appears,
  and may never reach an operator verb at all;
* the operator is limited to ``operator_answer``, ``operator_csrf`` and
  ``whoami``, and must name an allow-listed user (exact, case-sensitive);
* the write additionally needs the configured ``Origin`` verbatim and a
  CSRF token minted for that user by ``GET /v1/operator_csrf``.

Nothing here raises a message that carries a header value, a token or the key:
a refusal names only the check that failed, which is also what lands in the
audit log.
"""
from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from .errors import Refused

__all__ = ["HDR_USER", "HDR_CSRF", "csrf_key", "csrf_token", "csrf_valid",
           "csrf_payload", "gate", "answer", "HopRefused"]

HDR_USER = "X-PG-Operator-User"
HDR_CSRF = "X-PG-CSRF"
WINDOW_S = 3600
MIN_KEY_BYTES = 32

#: The doors the operator may open, and the operator-only doors nobody else
#: may even name.
OPERATOR_VERBS = frozenset({"operator_answer", "operator_csrf"})
ANSWER_FIELDS = frozenset({"key", "choice_id", "text"})


class HopRefused(Refused):
    """The hop gate said no: the reason names the check, never a value."""

    def __init__(self, reason):
        super().__init__("forbidden", f"operator hop refused: {reason}")


def _values(headers, name):
    """Every value of *name*, so a repeated header can never be read as one."""
    return list(headers.get_all(name) or [])


def csrf_key(config):
    """The MAC key bytes, or ``None`` when unusable — it fails closed."""
    path = getattr(config, "operator_csrf_key_file", None)
    if not path:
        return None
    try:
        raw = Path(path).read_text(encoding="utf-8").strip().encode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return raw if len(raw) >= MIN_KEY_BYTES else None


def csrf_token(key_bytes, user, now) -> str:
    """The token *user* presents within the window ``now`` falls in."""
    message = f"{user}|{int(now // WINDOW_S)}".encode("utf-8")
    return hmac.new(key_bytes, message, hashlib.sha256).hexdigest()


def csrf_valid(key_bytes, user, token, now) -> bool:
    """Current or previous window only; a missing key or shape fails closed."""
    if not key_bytes or not isinstance(token, str) or not isinstance(user, str):
        return False
    return any(hmac.compare_digest(csrf_token(key_bytes, user, moment), token)
               for moment in (now, now - WINDOW_S))


#: Each door is served on exactly one method.
ALLOWED_METHODS = {"operator_answer": "POST", "operator_csrf": "GET",
                   "whoami": "GET"}


def gate(principal, name, method, headers, config, now):
    """The operator user for this request, or ``None`` for a normal one.

    Raises :class:`HopRefused` (a 403) when the hop does not hold.
    """
    role = getattr(principal, "role", None)
    users = _values(headers, HDR_USER)
    if role != "operator":
        if users or name in OPERATOR_VERBS:
            raise HopRefused("not the operator")
        return None
    if ALLOWED_METHODS.get(name) != method:
        raise HopRefused("verb")
    if len(users) != 1:
        raise HopRefused("user header")
    user = users[0]
    if user not in (getattr(config, "operator_users", None) or []):
        raise HopRefused("unknown user")
    if name == "operator_answer":
        origin = getattr(config, "operator_origin", None)
        if not origin or _values(headers, "Origin") != [origin]:
            raise HopRefused("origin")
        token = _values(headers, HDR_CSRF)
        key = csrf_key(config)
        if len(token) != 1 or not csrf_valid(key, user, token[0], now):
            raise HopRefused("csrf")
    return user


def csrf_payload(config, user, now):
    """``GET /v1/operator_csrf``: a token for the gate's own user only."""
    key = csrf_key(config)
    if key is None:
        raise HopRefused("no csrf key")
    return {"csrf": csrf_token(key, user, now)}


def answer(store, principal, body, user):
    """``POST /v1/operator_answer``: key, choice_id and text, nothing else."""
    extra = set(body) - ANSWER_FIELDS
    if extra:
        raise Refused("validation", f"unexpected fields {sorted(extra)}")
    key = body.get("key")
    if not isinstance(key, str) or not key:
        raise Refused("validation", "missing field 'key'")
    return store.operator_answer(principal, key, body.get("choice_id"),
                                 body.get("text"), user=user)
