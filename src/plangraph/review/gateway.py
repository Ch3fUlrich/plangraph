"""Tool-less review call against the gateway (stdlib only)."""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plangraph.catalog_data import SEED_MODELS

__all__ = ["DEFAULT_MAX_TOKENS", "GATEWAY_ENV_VAR",
           "MissingKey", "MissingGatewayURL", "CallResult", "request_body",
           "request_headers", "gateway_base_url", "model_ceiling",
           "clamp_max_tokens", "call", "write_seat_dir"]

DEFAULT_MAX_TOKENS = 128000
GATEWAY_ENV_VAR = "PLANGRAPH_REVIEW_URL"
SESSION_HEADER = "x-review-session-id"
MAX_ERROR_SNIPPET = 200
CORRELATION_HEADER_NAMES = ("x-correlation-id", "correlation-id",
                            "x-review-correlation-id", "x-request-id", "request-id")
_SESSION_TAG_RE = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")


class MissingKey(RuntimeError):
    """Review key env var missing (names the var only)."""


class MissingGatewayURL(MissingKey):
    """Review gateway URL missing (names the var only)."""


def utc_now() -> str:
    """Current UTC time as YYYY-MM-DDTHH:MM:SSZ."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gateway_base_url(url: str) -> str:
    """API root: path-less URL gains /v1, existing path kept."""
    parts = urllib.parse.urlsplit((url or "").rstrip("/"))
    path = parts.path if parts.path not in ("", "/") else "/v1"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def slugify(text: str, cap: int = 24) -> str:
    """Lowercase [a-z0-9-] slug, capped, never empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:cap].strip("-")
    return slug or "review"


def session_tag(title: str | None, prompt_sha256: str) -> str:
    """review/<title> when header-safe, else review/<sha12>."""
    candidate = (title or "").strip()
    if candidate and _SESSION_TAG_RE.match("review/" + candidate):
        return "review/" + candidate
    return "review/" + (prompt_sha256 or "")[:12]


def mint_run_id(slug: str) -> str:
    """UTC YYYYMMDD-HHMMSS-<slug>-<hex6> run id."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return "%s-%s-%s" % (now.strftime("%Y%m%d-%H%M%S"), slug, secrets.token_hex(3))


def mask_key(text: str, key: str) -> str:
    """Redact key occurrences before storing or writing."""
    return text.replace(key, "[redacted]") if key and text else text


def model_ceiling(model: str, requested: int = DEFAULT_MAX_TOKENS) -> int:
    """Catalog max_output_tokens for a known model id; else DEFAULT_MAX_TOKENS.

    A known entry with a missing or invalid ``max_output_tokens`` falls back
    to min(DEFAULT_MAX_TOKENS, requested) -- never a bare 128000 when the
    caller asked for less. An unknown model id caps at DEFAULT_MAX_TOKENS.
    """
    for entry in SEED_MODELS:
        if entry.get("model_id") == model:
            value = entry.get("max_output_tokens")
            if (isinstance(value, int) and not isinstance(value, bool)
                    and value > 0):
                return value
            return min(DEFAULT_MAX_TOKENS, int(requested))
    return DEFAULT_MAX_TOKENS


def clamp_max_tokens(model: str, max_tokens: int) -> int:
    """min(requested, model ceiling); unknown models cap at DEFAULT_MAX_TOKENS.

    A known entry with a missing or invalid ``max_output_tokens`` falls back
    to min(DEFAULT_MAX_TOKENS, requested).
    """
    return min(int(max_tokens), model_ceiling(model, int(max_tokens)))


def request_body(model: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS,
                 temperature: float | None = None) -> dict:
    """Tool-less body: stream off, no tools, temperature only if given."""
    body: dict[str, Any] = {"model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "stream": False, "max_tokens": max_tokens}
    if temperature is not None:
        body["temperature"] = temperature
    return body


def request_headers(key: str, tag: str, run_id: str) -> dict:
    """Bearer auth plus the review session header."""
    value = "%s/%s" % (tag, run_id) if run_id else tag
    return {"Authorization": "Bearer " + key, "Content-Type": "application/json",
            SESSION_HEADER: value}


def _answer_text(payload: dict) -> str:
    """Assistant text from string or list-of-parts content."""
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _finish_of(payload: dict) -> str | None:
    """choices[0].finish_reason verbatim, else None."""
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    reason = choices[0].get("finish_reason")
    return reason if isinstance(reason, str) else None


def _correlation(headers: Any) -> tuple[str | None, list[str]]:
    """First correlation header value plus all header names."""
    items = list(headers.items())
    lowered = {n.lower(): v for n, v in items}
    for candidate in CORRELATION_HEADER_NAMES:
        if lowered.get(candidate):
            return lowered[candidate], [n for n, _ in items]
    for name, value in items:
        if "correlation" in name.lower() and value:
            return value, [n for n, _ in items]
    return None, [n for n, _ in items]


@dataclass
class CallResult:
    """Outcome of one gateway call (key never stored)."""
    status: int
    text: str
    finish_reason: str | None
    served_model: str | None
    requested_model: str
    correlation_id: str | None
    response_header_names: list[str] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)
    prompt_sha256: str = ""
    started: str = ""
    finished: str = ""
    error: str | None = None
    session_tag: str = ""
    run_id: str = ""
    truncated_empty: bool = False
    attempts: list[dict] = field(default_factory=list)

    def to_evidence(self) -> dict:
        """Evidence fields (caller adds verdict/test-gaming)."""
        return {"requested_model": self.requested_model, "served_model": self.served_model,
                "status": self.status, "finish_reason": self.finish_reason,
                "correlation_id": self.correlation_id,
                "response_header_names": list(self.response_header_names),
                "session_tag": self.session_tag, "run_id": self.run_id,
                "prompt_sha256": self.prompt_sha256, "tokens": dict(self.tokens),
                "started": self.started, "finished": self.finished,
                "truncated_empty": self.truncated_empty,
                "attempts": [dict(a) for a in self.attempts]}


def _blank(model: str, digest: str, tag: str, run_id: str, started: str,
           finished: str, status: int, error: str | None,
           names: list[str] | None = None) -> CallResult:
    """Empty-text result for error paths."""
    return CallResult(status=status, text="", finish_reason=None, served_model=None,
                      requested_model=model, correlation_id=None,
                      response_header_names=list(names or []), tokens={},
                      prompt_sha256=digest, started=started, finished=finished,
                      error=error, session_tag=tag, run_id=run_id)


def _attempt_record(n: int, sent: int, result: CallResult) -> dict:
    """Compact record of one wire attempt (key never stored)."""
    return {"attempt": n, "max_tokens": sent, "status": result.status,
            "finish_reason": result.finish_reason, "served_model": result.served_model,
            "correlation_id": result.correlation_id,
            "text_empty": not (result.text or "").strip(),
            "started": result.started, "finished": result.finished}


def _post(model: str, prompt: str, base: str, key: str, tag: str, run_id: str,
          digest: str, max_tokens: int, timeout: int) -> CallResult:
    """POST one tool-less completion; HTTP/network errors become values."""
    started = utc_now()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(request_body(model, prompt, max_tokens)
                                                 ).encode("utf-8"),
                                 headers=request_headers(key, tag, run_id), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, resp_headers, raw_body = int(resp.status), resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            raw_error = exc.read()
        except OSError:
            raw_error = b""
        names = [n for n, _ in exc.headers.items()] if exc.headers is not None else []
        snippet = mask_key(raw_error.decode("utf-8", "replace"), key)[:MAX_ERROR_SNIPPET]
        return _blank(model, digest, tag, run_id, started, utc_now(),
                      int(exc.code), "HTTP %d: %s" % (int(exc.code), snippet), names)
    except Exception as exc:  # noqa: BLE001 - network/timeout become status 0
        return _blank(model, digest, tag, run_id, started, utc_now(),
                      0, type(exc).__name__)
    finished = utc_now()
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("response is not a JSON object")
    except (UnicodeDecodeError, ValueError) as exc:
        return _blank(model, digest, tag, run_id, started, finished, 0, type(exc).__name__)
    served = payload.get("model")
    if served is not None and not isinstance(served, str):
        served = str(served)
    usage = payload.get("usage")
    cid, names = _correlation(resp_headers)
    return CallResult(status=status, text=mask_key(_answer_text(payload), key),
                      finish_reason=_finish_of(payload), served_model=served,
                      requested_model=model, correlation_id=cid,
                      response_header_names=names, tokens=dict(usage or {}),
                      prompt_sha256=digest, started=started, finished=finished,
                      error=None, session_tag=tag, run_id=run_id)


def call(model: str, prompt: str, *, url: str | None = None,
         key_env: str = "PLANGRAPH_REVIEW_KEY", max_tokens: int = DEFAULT_MAX_TOKENS,
         timeout: int = 1200, title: str | None = None, env: Any | None = None,
         ) -> CallResult:
    """POST one tool-less completion; HTTP/network errors become values.

    ``max_tokens`` is clamped to the model's catalog ``max_output_tokens``
    (a DEFAULT_MAX_TOKENS ceiling for an unknown model; a known entry with
    a missing or invalid ``max_output_tokens`` falls back to
    min(DEFAULT_MAX_TOKENS, requested)). A reply with
    ``finish_reason == "length"`` and empty text retries ONCE at that
    ceiling; the result is marked ``truncated_empty`` (never ok, never
    skipped) only when the retry itself returned status 200 with
    ``finish_reason == "length"`` and empty text -- otherwise the retry's
    own error stands. Every attempt is recorded in ``CallResult.attempts``.
    """
    source = env if env is not None else os.environ
    key = (source.get(key_env) or "").strip() if source is not None else ""
    if not key:
        raise MissingKey("missing review key: set %s" % key_env)
    base_url = url or (source.get(GATEWAY_ENV_VAR) or "").strip()
    if not base_url:
        raise MissingGatewayURL("missing review gateway URL: set %s" % GATEWAY_ENV_VAR)
    base = gateway_base_url(base_url)
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    tag = session_tag(title, digest)
    run_id = mint_run_id(slugify(title or "review"))
    ceiling = model_ceiling(model, int(max_tokens))
    attempts: list[dict] = []
    sent = clamp_max_tokens(model, max_tokens)
    result = _post(model, prompt, base, key, tag, run_id, digest, sent, timeout)
    attempts.append(_attempt_record(1, sent, result))
    if result.finish_reason == "length" and not (result.text or "").strip():
        result = _post(model, prompt, base, key, tag, run_id, digest, ceiling, timeout)
        attempts.append(_attempt_record(2, ceiling, result))
        if (result.status == 200 and result.finish_reason == "length"
                and not (result.text or "").strip()):
            result.truncated_empty = True
    result.attempts = attempts
    return result


def write_seat_dir(out_dir: Path, result: CallResult, extra: dict | None = None) -> None:
    """Write review.txt + evidence.json, plus error.json when not 200 or truncated_empty.

    A ``truncated_empty`` result is never a normal review: error.json is
    always written for it with ``error: "truncated_empty"`` and the attempt
    list, even at status 200.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.txt").write_text(result.text, encoding="utf-8", newline="\n")
    evidence = result.to_evidence()
    if extra:
        evidence.update(extra)
    (out / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n",
                                       encoding="utf-8", newline="\n")
    if result.status != 200 or result.truncated_empty:
        doc = {"status": result.status,
               "error": ("truncated_empty" if result.truncated_empty
                         else result.error),
               "requested_model": result.requested_model,
               "response_header_names": list(result.response_header_names),
               "session_tag": result.session_tag, "run_id": result.run_id,
               "prompt_sha256": result.prompt_sha256, "started": result.started,
               "finished": result.finished,
               "attempts": [dict(a) for a in result.attempts]}
        (out / "error.json").write_text(json.dumps(doc, indent=2) + "\n",
                                        encoding="utf-8", newline="\n")
