"""Shared secret masking for the hooks and tools/autoos_refs_check.py.

This repository is public and the masked text travels (an inbox, a parent's
context, an operator's terminal), so the rules stay deliberately greedy: a
missed secret leaks, an over-eager mask only costs a readable word.
`redact_credentials(text, mask)` masks, anywhere in a line and in any case:

* the VALUE of `KEY=VALUE`, `KEY: VALUE`, `"KEY": "VALUE"` and `KEY => VALUE`
  when KEY contains pass, secret, token, key, dsn, auth or cred. The pair is
  found wherever it sits: nested in another assignment
  (`Environment=NAME=value`, `Environment="NAME=value with spaces"`), in a
  query string, in JSON escaped inside a string, or behind a flag
  (`--password VALUE`). An HTTP scheme word (`Bearer`, `Basic`, ...) belongs
  to the value, so `Authorization: Basic <b64>` is masked whole;
* URL userinfo: `scheme://user:pass@host` becomes `scheme://user:***@host`;
* well-known key shapes: sk-, ghp_, github_pat_, xox*, AKIA, JWTs, Bearer.

The result is idempotent: a masked line passes through unchanged, so a
consumer can mask on write and again on read. Stdlib only, portable.
"""
from __future__ import annotations

import re

#: A key is secret when it contains one of these anywhere ("OPENAI_API_KEY",
#: "AUTOOS_PG_DSN", "X-Auth-Token", "dbPassword"); the check ignores case.
SECRET_WORDS = ("pass", "secret", "token", "key", "dsn", "auth", "cred")

# Everyday words that carry a secret word without being one ("tests passed: 41",
# "author: ...", "keyboard=us"); they are blanked from a key before the check,
# so a status line keeps its evidence. "authoriz..." stays secret.
_BENIGN = re.compile(
    r"pass(?:ed|es|ing|through)|bypass|compass|author(?!iz)|credit|keyboard"
    r"|keyword|monkey|turkey")

#: The default mask for a VALUE; a URL password always becomes URL_MASK.
MASK = "[redacted]"
URL_MASK = "***"

_KEY = r"A-Za-z0-9_.\-"

# KEY, an optional (possibly escaped) closing quote, then the separator. KEY is
# a maximal run of key characters, so each run is tried once (linear); the
# VALUE is read separately, which lets a non-secret leading key
# (`Environment=`) fall through to the pair nested behind it.
_PAIR = re.compile(
    r"(?<![%s])(?P<key>[%s]+)(?P<kq>(?:\\?[\"'])?)"
    r"(?P<sep>[ \t]*(?:=>|:=|==|[:=])[ \t]*)" % (_KEY, _KEY))

_SCHEME = r"(?:Bearer|Basic|Digest|Negotiate|NTLM|Token)[ \t]+"
# VALUE: an optional auth scheme word, then a quoted string (an unterminated
# one runs to the end of the line), an escaped-quote string (JSON inside a
# string) or a bare run.
_VALUE = re.compile(
    r"(?:%s)?(?:\"(?:[^\"\\\r\n]|\\.)*\"?|'(?:[^'\\\r\n]|\\.)*'?"
    r"|\\(?P<q>[\"'])(?:(?!\\(?P=q))[^\r\n])*(?:\\(?P=q))?|[^\s\"']+)" % _SCHEME,
    re.IGNORECASE)

# `Environment="NAME=value with spaces"`: the quote opens before the key, so the
# value runs to the closing quote.
_UNTIL_QUOTE = {'"': re.compile(r"(?:[^\"\\\r\n]|\\.)*"),
                "'": re.compile(r"(?:[^'\\\r\n]|\\.)*")}

# `--password VALUE` (the value is the next word unless that is another flag).
_FLAG = re.compile(
    r"(?<![\w-])(?P<flag>--?[A-Za-z][A-Za-z0-9_-]*)(?P<sp>[ \t]+)"
    r"(?P<val>\"[^\"\r\n]*\"?|'[^'\r\n]*'?|[^\s\-\"'][^\s]*)")

# scheme://[user]:PASSWORD@host - greedy to the last '@' before the first '/',
# so a raw '@', ':', '?' or '#' inside the password is masked too.
_USERINFO = re.compile(
    r"(?<![A-Za-z0-9+.\-])(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://[^\s/?#@:\"'<>]*:)"
    r"[^\s/\"'<>]+(?=@)")

_SHAPES = tuple(re.compile(p) for p in (
    r"sk[-_][A-Za-z0-9_-]{16,}",
    r"\bsk[-_][A-Za-z0-9_-]{4,}",
    r"gh[pousr]_[A-Za-z0-9]+",
    r"github_pat_[A-Za-z0-9_]+",
    r"xox[abprs]-[A-Za-z0-9-]+",
    r"(?:AKIA|ASIA)[0-9A-Z]{16}",
    r"(?i:Bearer)\s+[A-Za-z0-9._~+/=-]+",
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
))


def is_secret_key(name: str) -> bool:
    """True when the key `name` contains a secret word (any case, anywhere)."""
    lowered = _BENIGN.sub("", name.lower())
    return any(word in lowered for word in SECRET_WORDS)


def _value_end(text: str, key_start: int, kq: str, start: int) -> int:
    """Index where the VALUE that begins at `start` ends (`start` when empty)."""
    lead = text[key_start - 1] if key_start else ""
    if lead in ("'", '"') and not kq:  # wrapped: Environment="NAME=value"
        if key_start > 1 and text[key_start - 2] == "\\":  # inside a JSON string
            eol = text.find("\n", start)
            limit = len(text) if eol < 0 else eol
            end = text.find("\\" + lead, start, limit)
            return limit if end < 0 else end
        return _UNTIL_QUOTE[lead].match(text, start).end()
    found = _VALUE.match(text, start)
    return found.end() if found else start


def _mask_pairs(text: str, mask: str) -> str:
    out: list[str] = []
    pos = 0
    for found in _PAIR.finditer(text):
        key_start = found.start("key")
        if key_start < pos or not is_secret_key(found.group("key")):
            continue  # inside a value already masked, or not a secret key
        start = found.end()
        end = _value_end(text, key_start, found.group("kq"), start)
        if text.startswith(mask, start):
            end = min(end, start + len(mask))  # already masked: keep what follows
        if end > start:
            out += (text[pos:start], mask)
            pos = end
    out.append(text[pos:])
    return "".join(out)


def _mask_flag(found: re.Match, mask: str) -> str:
    if not is_secret_key(found.group("flag")):
        return found.group(0)
    return found.group("flag") + found.group("sp") + mask


def redact_credentials(text, mask: str = MASK) -> str:
    """`text` with every credential value replaced by `mask` (see the module doc).

    A non-string is "" so a hand-edited record cannot raise. URL passwords
    become `***` whatever `mask` is.
    """
    if not isinstance(text, str):
        return ""
    for shape in _SHAPES:
        text = shape.sub(lambda _found: mask, text)
    text = _mask_pairs(text, mask)
    text = _FLAG.sub(lambda found: _mask_flag(found, mask), text)
    return _USERINFO.sub(lambda found: found.group("head") + URL_MASK, text)
