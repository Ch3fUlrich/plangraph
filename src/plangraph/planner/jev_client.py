"""Jev API client and deterministic stub for the planner shadow hook.

Jev is TypeSafe AI's "System One" decision model: no text output, just a
calibrated choice plus probabilities.  It runs here in *shadow* - it answers the
same question as the rules router and the answer is only recorded, never used to
change a decision - so a slow, failing or absent Jev must never block anything.

Two ways to answer:

* live HTTP against ``cfg.url`` (``urllib`` only) with a bearer key resolved at
  call time by :func:`resolve_key`;
* :func:`stub_answer`, a deterministic local answerer used by mode ``"stub"`` and
  as the mode ``"auto"`` fallback when no key is found.

``DEFAULT_URL`` / ``DEFAULT_MODEL`` and the ``zen`` profile are UNVERIFIED
defaults: the wire format is documented, but the Zen proxy path and the model
name are not confirmed here.  No API key exists on this workstation, so the
tests never make a real call; they point ``cfg.url`` at a fake server on
127.0.0.1 or inject an ``opener``.

The key VALUE is read from the source named by ``cfg.key_source`` only at call
time, is never cached, is never written to ``os.environ``, a file or a row, and
never appears in a log line, an exception message, a reason string or a
``repr``.  ``JevConfig`` and ``JevAnswer`` never hold the key.

Test-safety guard: while pytest is running (``PYTEST_CURRENT_TEST`` present in the
REAL ``os.environ``) ``auth_store`` never reads a default location, so a test can
never pick up a real key from the machine's own credential store and make a real
network call.  Only an explicit override - ``PLANGRAPH_JEV_AUTH_PATH`` for the
``auth.json``, ``PLANGRAPH_JEV_AUTH_DB`` for the sqlite store, which the tests
point at a ``tmp_path`` file - reaches the filesystem, and the two sources are
guarded independently.  The marker is read from ``os.environ`` and never from the
caller's ``env`` mapping, so a test cannot lift the guard by passing its own
environment.  Production (no ``PYTEST_CURRENT_TEST``) is unaffected.

The ``auth.json`` shape and the sqlite ``credential`` column names below are
ASSUMPTIONS about the local opencode credential store, verified only by the live
smoke the operator runs; every read failure degrades to ``None`` and is silent.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, replace

__all__ = ["ENV_MODE", "ENV_URL", "ENV_MODEL", "ENV_TIMEOUT", "ENV_PROFILE",
           "ENV_KEY_SOURCE", "ENV_AUTH_PATH", "ENV_AUTH_DB", "DEFAULT_URL",
           "DEFAULT_MODEL", "DEFAULT_TIMEOUT_S", "MAX_STATE_CHARS",
           "MAX_RESPONSE_BYTES", "MODES", "PROFILES", "DEFAULT_PROFILE",
           "JevConfig", "JevAnswer", "config_from_env", "enabled", "resolve_key",
           "ask", "stub_answer", "parse_answer", "mask"]

#: Master switch: ``off`` (default), ``stub``, ``api`` or ``auto``/``on``.
ENV_MODE = "PLANGRAPH_JEV"
#: Base URL override (also the per-profile default override).
ENV_URL = "PLANGRAPH_JEV_URL"
#: Model-name override.
ENV_MODEL = "PLANGRAPH_JEV_MODEL"
#: Request timeout in seconds (float, clamped to <= 60).
ENV_TIMEOUT = "PLANGRAPH_JEV_TIMEOUT_S"
#: Profile selector: ``zen`` (default) or ``typesafe``; unknown -> zen.
ENV_PROFILE = "PLANGRAPH_JEV_PROFILE"
#: Key source override: ``auth_store`` or ``env:<NAME>``.
ENV_KEY_SOURCE = "PLANGRAPH_JEV_KEY_SOURCE"
#: Override for the local opencode ``auth.json`` path (auth_store only).
ENV_AUTH_PATH = "PLANGRAPH_JEV_AUTH_PATH"
#: Override for the local opencode sqlite credential store (auth_store only).
ENV_AUTH_DB = "PLANGRAPH_JEV_AUTH_DB"

#: UNVERIFIED zen-profile default (public vendor URL).
DEFAULT_URL = "https://opencode.ai/zen/v1/systemone"
#: UNVERIFIED zen-profile default model name.
DEFAULT_MODEL = "jev-1.13-free"
#: Default request timeout in seconds.
DEFAULT_TIMEOUT_S = 8.0
#: ``state`` is truncated to this many characters before it is sent.
MAX_STATE_CHARS = 12000
#: Response bodies larger than this many bytes are an error.
MAX_RESPONSE_BYTES = 1048576
#: Recognised modes; ``on`` is accepted as an alias of ``auto``.
MODES = ("off", "stub", "api", "auto")

#: Per-profile DEFAULTS only; individual ``ENV_*`` vars always override them.
PROFILES = {
    "zen": {"url": DEFAULT_URL, "model": DEFAULT_MODEL, "key_source": "auth_store"},
    "typesafe": {"url": "https://api.typesafe.ai/v1/systemone",
                 "model": "jev-latest", "key_source": "env:JEV_API_KEY"},
}
#: Profile used when ``ENV_PROFILE`` is unset or unknown.
DEFAULT_PROFILE = "zen"

#: Provider names tried, in order, inside the opencode ``auth.json``.
_AUTH_PROVIDERS = ("opencode", "opencode-zen", "zen")
#: Value fields tried, in order, inside an auth entry / credential row.
_SECRET_FIELDS = ("key", "access", "token")
#: Secret column names tried, in order, on the sqlite ``credential`` table.
_SECRET_COLUMNS = ("key", "api_key", "secret", "token", "access", "value")
#: Integration/provider column names tried, in order.
_INTEGRATION_COLUMNS = ("integration", "provider", "integration_id")
#: Integration value that marks the credential this client wants.
_INTEGRATION_VALUE = "opencode-go"
#: Fixed phrase used for every reason that only reports a failure class.
_REASON_CAP = 120


def _env(env: Mapping | None) -> Mapping:
    """The caller's mapping, or ``os.environ`` when none is given."""
    return os.environ if env is None else env


def mask(text, key) -> str:
    """Replace every occurrence of ``key`` in ``text`` with ``***``.

    No-op when either argument is empty or not a string; this is defence in
    depth so a key value can never reach a reason string.
    """
    if not isinstance(text, str) or not isinstance(key, str) or not key:
        return text
    return text.replace(key, "***")


def _reason(text, key) -> str:
    """Masked, length-capped reason string (never built from ``str(exc)``)."""
    value = mask(text if isinstance(text, str) else str(text), key)
    return value[:_REASON_CAP]


def _model_of(cfg) -> str:
    """``cfg.model`` when it is a usable string, else the documented default."""
    model = getattr(cfg, "model", None)
    return model if isinstance(model, str) and model else DEFAULT_MODEL


def enabled(cfg) -> bool:
    """True unless the mode is ``off``; anything else runs (``ask`` sanitises modes)."""
    return getattr(cfg, "mode", "off") != "off"


def _timeout(raw) -> float:
    """Parse a timeout; invalid, non-positive or non-finite -> ``DEFAULT_TIMEOUT_S``."""
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    if not value > 0 or value == float("inf"):
        return DEFAULT_TIMEOUT_S
    return min(value, 60.0)


@dataclass(frozen=True)
class JevConfig:
    """Resolved Jev configuration; holds no key (``repr`` is safe)."""
    mode: str = "off"
    url: str = DEFAULT_URL
    model: str = DEFAULT_MODEL
    key_source: str = "auth_store"
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class JevAnswer:
    """One shadow answer: a status, the parsed choice and its reason."""
    status: str = "error"
    choice: str | None = None
    probabilities: dict | None = None
    confidence: float | None = None
    reason: str = ""
    model: str = ""
    latency_ms: int | None = None


def config_from_env(env: Mapping | None = None) -> JevConfig:
    """Read a :class:`JevConfig` from ``env`` (default ``os.environ``).

    The profile supplies defaults for url/model/key_source only; each individual
    ``ENV_*`` var overrides it.  An unreadable mode becomes ``off``; ``on`` is
    the documented alias of ``auto``.
    """
    source = _env(env)
    mode = str(source.get(ENV_MODE, "") or "").strip().lower()
    if mode == "on":
        mode = "auto"
    if mode not in MODES:
        mode = "off"
    profile = str(source.get(ENV_PROFILE, "") or "").strip().lower()
    defaults = PROFILES.get(profile, PROFILES[DEFAULT_PROFILE])
    url = str(source.get(ENV_URL, "") or "").strip() or defaults["url"]
    model = str(source.get(ENV_MODEL, "") or "").strip() or defaults["model"]
    key_source = (str(source.get(ENV_KEY_SOURCE, "") or "").strip()
                  or defaults["key_source"])
    return JevConfig(mode=mode, url=url, model=model,
                     key_source=key_source, timeout_s=_timeout(source.get(ENV_TIMEOUT)))


def _data_dir(env: Mapping) -> str:
    """``XDG_DATA_HOME`` when set, else the conventional ``~/.local/share``."""
    xdg = str(env.get("XDG_DATA_HOME", "") or "").strip()
    if xdg:
        return xdg
    return os.path.join(os.path.expanduser("~"), ".local", "share")


def _candidates(env: Mapping, env_name: str, filename: str) -> list[str]:
    """Paths to try for a credential file, in order.

    An explicit ``env_name`` override is authoritative; otherwise the XDG data
    dir is tried first and, on Windows only, ``%LOCALAPPDATA%`` as a fallback.
    """
    override = str(env.get(env_name, "") or "").strip()
    if override:
        return [override]
    paths = [os.path.join(_data_dir(env), "opencode", filename)]
    if os.name == "nt":
        local = str(env.get("LOCALAPPDATA", "") or "").strip()
        if local:
            paths.append(os.path.join(local, "opencode", filename))
    return paths


def _default_location_blocked(env: Mapping, env_name: str) -> bool:
    """True when a DEFAULT (non-override) credential location must be skipped.

    Test-safety guard: while pytest is running we refuse to read the machine's
    real credential store, so a test that forgets its override cannot pick up a
    real key and make a real network call.  ``env_name`` is the override that
    lifts the guard for one source (``ENV_AUTH_PATH`` / ``ENV_AUTH_DB``); the two
    sources are guarded independently.

    The pytest marker is read from the REAL ``os.environ``, never from the
    caller's ``env`` mapping: passing an ``env`` without the marker must not let a
    test back into the default locations.
    """
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return False
    return not str(env.get(env_name, "") or "").strip()


def _first_secret(entry):
    """Secret from an auth entry: a non-empty string, or its first key/access/token."""
    if isinstance(entry, str):
        text = entry.strip()
        return text or None
    if isinstance(entry, Mapping):
        for field in _SECRET_FIELDS:
            value = entry.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _auth_json_secret(env: Mapping) -> str | None:
    """Read the key from the opencode ``auth.json``, fresh, or ``None``."""
    for path in _candidates(env, ENV_AUTH_PATH, "auth.json"):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, Mapping):
            continue
        for provider in _AUTH_PROVIDERS:
            secret = _first_secret(data.get(provider))
            if secret:
                return secret
    return None


def _ident(name) -> str:
    """Double-quote a column/table identifier taken from PRAGMA output."""
    return '"%s"' % str(name).replace('"', '""')


def _pick(columns, candidates):
    """First candidate column name present (case-insensitively), else None."""
    lowered = {str(column).lower(): column for column in columns}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def _truthy(value) -> bool:
    """Truthiness of an sqlite value, treating the usual false spellings as false."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (bytes, bytearray)):
        return bool(bytes(value).strip())
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no")
    return bool(value)


def _secret_from_value(value) -> str | None:
    """A credential cell: a string, or a JSON object string holding key/access/token."""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[:1] in "{[":
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, Mapping):
            inner = _first_secret(parsed)
            if inner:
                return inner
    return text


def _credential_db_secret(path) -> str | None:
    """Read the active ``opencode-go`` credential from one sqlite db, read-only."""
    connection = None
    try:
        uri = "file:%s?mode=ro" % str(path).replace("\\", "/")
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        cursor = connection.cursor()
        try:
            cursor.execute('PRAGMA table_info("credential")')
            info = cursor.fetchall()
        except sqlite3.Error:
            return None
        columns = [row[1] for row in info if len(row) > 1]
        if not columns:
            return None
        secret_column = _pick(columns, _SECRET_COLUMNS)
        if secret_column is None:
            return None
        active_column = _pick(columns, ("active",))
        integration_column = _pick(columns, _INTEGRATION_COLUMNS)
        selected = [_ident(secret_column)]
        if active_column is not None:
            selected.append(_ident(active_column))
        sql = "SELECT %s FROM %s" % (", ".join(selected), _ident("credential"))
        params: list = []
        if integration_column is not None:
            sql += " WHERE %s = ?" % _ident(integration_column)
            params.append(_INTEGRATION_VALUE)
        try:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        except sqlite3.Error:
            return None
        for row in rows:
            value = row[0]
            if active_column is not None and len(row) > 1 and not _truthy(row[1]):
                continue
            secret = _secret_from_value(value)
            if secret:
                return secret
        return None
    except (sqlite3.Error, OSError, ValueError, UnicodeError):
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def resolve_key(cfg, env: Mapping | None = None) -> str | None:
    """Resolve the bearer key at call time; never raises, never caches.

    ``env:<NAME>`` reads ``env[NAME]`` (stripped; empty -> ``None``).
    ``auth_store`` reads the opencode ``auth.json`` and, if that yields nothing,
    the opencode sqlite credential store.  Every failure (missing file, bad JSON,
    wrong shape, ``OSError``, locked or absent table) becomes ``None``.

    Test-safety guard (see the module docstring): under pytest an ``auth_store``
    DEFAULT location is skipped unless its override is set, so the tests can only
    read a store they pointed at themselves.
    """
    try:
        source = _env(env)
        key_source = str(getattr(cfg, "key_source", "") or "").strip()
        if key_source.startswith("env:"):
            name = key_source[4:].strip()
            if not name:
                return None
            value = source.get(name)
            text = value.strip() if isinstance(value, str) else ""
            return text or None
        if key_source == "auth_store":
            # Under pytest the default home/data-dir locations are never read;
            # an explicit override still is (per source, independently).
            if not _default_location_blocked(source, ENV_AUTH_PATH):
                secret = _auth_json_secret(source)
                if secret:
                    return secret
            if not _default_location_blocked(source, ENV_AUTH_DB):
                for path in _candidates(source, ENV_AUTH_DB, "opencode.db"):
                    secret = _credential_db_secret(path)
                    if secret:
                        return secret
            return None
        return None
    except Exception:  # noqa: BLE001 - a read failure is "no key", never a crash
        return None


def _ms_since(started: float) -> int:
    """Elapsed milliseconds since ``started`` (``time.monotonic``)."""
    return max(0, int((time.monotonic() - started) * 1000))


def _seconds(value) -> str:
    """Short human-readable timeout number for a reason string."""
    try:
        return "%g" % float(value)
    except (TypeError, ValueError):
        return "%g" % DEFAULT_TIMEOUT_S


def _keys(questions) -> list:
    """Question keys in insertion order (``[]`` when not a mapping)."""
    return list(questions.keys()) if isinstance(questions, Mapping) else []


def _items(questions):
    """``(key, question)`` pairs (``[]`` when not a mapping)."""
    return list(questions.items()) if isinstance(questions, Mapping) else []


def _criteria(question) -> Mapping:
    """The criteria map of a question, or an empty mapping."""
    if isinstance(question, Mapping):
        criteria = question.get("criteria")
        if isinstance(criteria, Mapping):
            return criteria
    return {}


def _question_payload(questions) -> dict:
    """Wire-format questions: ``{"<key>": {"type","instructions","criteria"}}``."""
    payload = {}
    for key, question in _items(questions):
        if isinstance(question, Mapping):
            qtype = question.get("type") or "choice"
            instructions = question.get("instructions")
            criteria = question.get("criteria")
        else:
            qtype, instructions, criteria = "choice", "", None
        payload[key] = {
            "type": qtype if isinstance(qtype, str) else "choice",
            "instructions": instructions if isinstance(instructions, str) else "",
            "criteria": dict(criteria) if isinstance(criteria, Mapping) else {},
        }
    return payload


def _answer(status, reason, cfg, key, *, model=None, latency_ms=None) -> JevAnswer:
    """Build one answer; model defaults to ``cfg.model`` and reason is masked."""
    return JevAnswer(status=status, choice=None, probabilities=None, confidence=None,
                     reason=_reason(reason, key), model=model or _model_of(cfg),
                     latency_ms=latency_ms)


def _fill(questions, status, reason, cfg, key, latency_ms=None, model=None) -> dict:
    """One answer with the same status/reason for every question key."""
    answer = _answer(status, reason, cfg, key, model=model, latency_ms=latency_ms)
    return {question_key: answer for question_key in _keys(questions)}


def parse_answer(question, raw, model, latency_ms, key=""):
    """Validate one 200-response answer against its question.

    ``raw`` is the raw per-key answer; ``key`` is the question key, used only to
    name the offending key in the reason.  Returns ``status="ok"`` with the parsed
    choice/probabilities/confidence, or ``status="error"`` with a fixed-phrase
    reason (``missing``, ``wrong type``, ``unknown choice``, ``bad probabilities``,
    ``bad confidence``).
    """
    label = key if isinstance(key, str) else str(key)

    def bad(what):
        return JevAnswer(status="error", choice=None, probabilities=None,
                         confidence=None,
                         reason=_reason("bad answer for %s: %s" % (label, what), ""),
                         model=model, latency_ms=latency_ms)

    criteria = _criteria(question)
    if not isinstance(raw, Mapping):
        return bad("missing")
    if raw.get("type") != "choice":
        return bad("wrong type")
    choice = raw.get("choice")
    if not isinstance(choice, str) or choice not in criteria:
        return bad("unknown choice")
    raw_probs = raw.get("probabilities")
    if not isinstance(raw_probs, Mapping):
        return bad("bad probabilities")
    probabilities: dict = {}
    total = 0.0
    for option, value in raw_probs.items():
        if option not in criteria:
            return bad("bad probabilities")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return bad("bad probabilities")
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return bad("bad probabilities")
        if number < 0.0 or number > 1.0:
            return bad("bad probabilities")
        probabilities[option] = number
        total += number
    if abs(total - 1.0) > 0.02:
        return bad("bad probabilities")
    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return bad("bad confidence")
    confidence = float(confidence)
    if confidence != confidence or confidence in (float("inf"), float("-inf")):
        return bad("bad confidence")
    return JevAnswer(status="ok", choice=choice, probabilities=probabilities,
                     confidence=confidence, reason="ok", model=model,
                     latency_ms=latency_ms)


def stub_answer(state: str, questions: dict) -> dict:
    """Deterministic offline answerer: no clock, no randomness, no network.

    For each question, a sha256 of ``"<key>\\0<state>"`` picks one criteria option;
    it gets ``0.6`` (``1.0`` when it is the only option) and the rest share
    ``0.4`` equally.  Answers carry ``model="stub"`` and ``reason="stub"``.
    """
    out: dict = {}
    state_text = state if isinstance(state, str) else str(state)
    for question_key, question in _items(questions):
        options = list(_criteria(question).keys())
        if not options:
            out[question_key] = JevAnswer(status="error", reason="no options",
                                          model="stub", latency_ms=0)
            continue
        digest = hashlib.sha256(
            ("%s\0%s" % (question_key, state_text)).encode("utf-8")).hexdigest()
        chosen = options[int(digest, 16) % len(options)]
        if len(options) == 1:
            probabilities = {chosen: 1.0}
            confidence = 1.0
        else:
            share = 0.4 / (len(options) - 1)
            probabilities = {option: (0.6 if option == chosen else share)
                             for option in options}
            confidence = 0.6
        out[question_key] = JevAnswer(status="ok", choice=chosen,
                                      probabilities=probabilities,
                                      confidence=confidence, reason="stub",
                                      model="stub", latency_ms=0)
    return out


def _stub_fallback(state, questions) -> dict:
    """Mode ``auto`` fallback: stub answers, marked as such in the reason."""
    answers = stub_answer(state, questions)
    return {key: (replace(answer, reason="stub (no key)")
                  if answer.status == "ok" else answer)
            for key, answer in answers.items()}


def _read_response(response):
    """Read at most ``MAX_RESPONSE_BYTES + 1`` bytes, then always close."""
    try:
        return response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - closing must never break the caller
                pass


def _status_of(response) -> int:
    """HTTP status of a response object, defaulting to 200."""
    try:
        return int(getattr(response, "status", None)
                   or getattr(response, "code", None) or 200)
    except (TypeError, ValueError):
        return 200


def _http_answers(questions, cfg, exc, key, started) -> dict:
    """Map an ``HTTPError`` to a status for every key of the request."""
    try:
        code = int(getattr(exc, "code", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    close = getattr(exc, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass
    latency = _ms_since(started)
    if code in (429, 529):
        return _fill(questions, "unavailable",
                     "http %d rate limited/overloaded" % code, cfg, key, latency)
    if code in (401, 403):
        return _fill(questions, "unavailable", "http %d auth" % code, cfg, key, latency)
    if 500 <= code < 600:
        return _fill(questions, "unavailable", "http %d" % code, cfg, key, latency)
    return _fill(questions, "error", "http %d" % code, cfg, key, latency)


def _url_answers(questions, cfg, exc, key, started) -> dict:
    """Map a ``URLError``: a timeout reason is a timeout, anything else a connect failure."""
    latency = _ms_since(started)
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return _fill(questions, "timeout",
                     "timeout after %ss" % _seconds(cfg.timeout_s), cfg, key, latency)
    return _fill(questions, "unavailable",
                 "connect failed: %s" % type(exc).__name__, cfg, key, latency)


def _timeout_answers(questions, cfg, key, started) -> dict:
    """Map a timeout (raw or wrapped) to ``timeout`` for every key."""
    return _fill(questions, "timeout",
                 "timeout after %ss" % _seconds(cfg.timeout_s), cfg, key,
                 _ms_since(started))


def _parse_body(questions, cfg, raw, key, latency) -> dict:
    """Parse a 200 body and build one answer per question key."""
    try:
        text = bytes(raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        payload = json.loads(text)
    except (UnicodeDecodeError, ValueError, TypeError):
        return _fill(questions, "error", "bad response body", cfg, key, latency)
    if not isinstance(payload, Mapping):
        return _fill(questions, "error", "bad response body", cfg, key, latency)
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        return _fill(questions, "error", "bad response body", cfg, key, latency)
    model = payload.get("model")
    if not (isinstance(model, str) and model):
        model = _model_of(cfg)
    model = model[:100]
    return {question_key: parse_answer(question, answers.get(question_key),
                                       model, latency, question_key)
            for question_key, question in _items(questions)}


def _live(state, questions, cfg, key, opener) -> dict:
    """One shadow HTTP call; every failure becomes a per-key answer."""
    started = time.monotonic()
    state_text = state if isinstance(state, str) else str(state)
    body = {"model": _model_of(cfg), "state": state_text[:MAX_STATE_CHARS],
            "questions": _question_payload(questions)}
    try:
        data = json.dumps(body).encode("utf-8")
    except (TypeError, ValueError):
        return _fill(questions, "error", "bad request body", cfg, key,
                     _ms_since(started))
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    request = urllib.request.Request(cfg.url, data=data, headers=headers, method="POST")
    call = opener if opener is not None else urllib.request.urlopen
    try:
        response = call(request, timeout=cfg.timeout_s)
    except urllib.error.HTTPError as exc:
        return _http_answers(questions, cfg, exc, key, started)
    except urllib.error.URLError as exc:
        return _url_answers(questions, cfg, exc, key, started)
    except (TimeoutError, socket.timeout):
        return _timeout_answers(questions, cfg, key, started)
    except (ConnectionError, OSError) as exc:
        return _fill(questions, "unavailable",
                     "connect failed: %s" % type(exc).__name__, cfg, key,
                     _ms_since(started))
    try:
        raw = _read_response(response)
    except urllib.error.HTTPError as exc:
        return _http_answers(questions, cfg, exc, key, started)
    except urllib.error.URLError as exc:
        return _url_answers(questions, cfg, exc, key, started)
    except (TimeoutError, socket.timeout):
        return _timeout_answers(questions, cfg, key, started)
    except (ConnectionError, OSError) as exc:
        return _fill(questions, "unavailable",
                     "connect failed: %s" % type(exc).__name__, cfg, key,
                     _ms_since(started))
    latency = _ms_since(started)
    if isinstance(raw, (bytes, bytearray)) and len(raw) > MAX_RESPONSE_BYTES:
        return _fill(questions, "error", "response too large", cfg, key, latency)
    return _parse_body(questions, cfg, raw, key, latency)


def _ask(state, questions, cfg, *, env=None, opener=None) -> dict:
    """Implementation of :func:`ask` (exceptions are caught by the public wrapper)."""
    mode = getattr(cfg, "mode", "off")
    mode = mode if isinstance(mode, str) else "off"
    if mode == "on":
        mode = "auto"
    if mode not in MODES or mode == "off":
        return {}
    if not isinstance(questions, Mapping):
        return {}
    if mode == "stub":
        return stub_answer(state, questions)
    key = resolve_key(cfg, env)
    if not key:
        if mode == "auto":
            return _stub_fallback(state, questions)
        return _fill(questions, "unavailable", "no key", cfg, None)
    return _live(state, questions, cfg, key, opener)


def ask(state: str, questions: dict, cfg: JevConfig, *, env=None,
        opener=None) -> dict:
    """Answer ``questions`` in shadow; always one :class:`JevAnswer` per key.

    Never raises: only ``Exception`` is caught (a bug becomes ``status="error"``,
    ``reason="client bug: <ClassName>"``), so ``KeyboardInterrupt`` and other
    ``BaseException`` subclasses still propagate.  Mode ``off`` returns ``{}``.
    The key is resolved at call time and never cached.
    """
    keys = _keys(questions)
    try:
        return _ask(state, questions, cfg, env=env, opener=opener)
    except Exception as exc:  # noqa: BLE001 - a client bug must not break shadow
        reason = _reason("client bug: %s" % type(exc).__name__, "")
        answer = JevAnswer(status="error", reason=reason, model=_model_of(cfg))
        return {question_key: answer for question_key in keys}
