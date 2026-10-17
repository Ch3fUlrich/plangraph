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

__all__ = ["DEFAULT_MAX_TOKENS", "GATEWAY_ENV_VAR",
           "MissingKey", "MissingGatewayURL", "CallResult", "request_body",
           "request_headers", "gateway_base_url", "call", "write_seat_dir"]

DEFAULT_MAX_TOKENS = 16000
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

    def to_evidence(self) -> dict:
        """Evidence fields (caller adds verdict/test-gaming)."""
        return {"requested_model": self.requested_model, "served_model": self.served_model,
                "status": self.status, "finish_reason": self.finish_reason,
                "correlation_id": self.correlation_id,
                "response_header_names": list(self.response_header_names),
                "session_tag": self.session_tag, "run_id": self.run_id,
                "prompt_sha256": self.prompt_sha256, "tokens": dict(self.tokens),
                "started": self.started, "finished": self.finished}


def _blank(model: str, digest: str, tag: str, run_id: str, started: str,
           finished: str, status: int, error: str | None,
           names: list[str] | None = None) -> CallResult:
    """Empty-text result for error paths."""
    return CallResult(status=status, text="", finish_reason=None, served_model=None,
                      requested_model=model, correlation_id=None,
                      response_header_names=list(names or []), tokens={},
                      prompt_sha256=digest, started=started, finished=finished,
                      error=error, session_tag=tag, run_id=run_id)


def call(model: str, prompt: str, *, url: str | None = None,
         key_env: str = "PLANGRAPH_REVIEW_KEY", max_tokens: int = DEFAULT_MAX_TOKENS,
         timeout: int = 1200, title: str | None = None, env: Any | None = None,
         ) -> CallResult:
    """POST one tool-less completion; HTTP/network errors become values."""
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


def write_seat_dir(out_dir: Path, result: CallResult, extra: dict | None = None) -> None:
    """Write review.txt + evidence.json (plus error.json when not 200)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.txt").write_text(result.text, encoding="utf-8", newline="\n")
    evidence = result.to_evidence()
    if extra:
        evidence.update(extra)
    (out / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n",
                                       encoding="utf-8", newline="\n")
    if result.status != 200:
        doc = {"status": result.status, "error": result.error,
               "requested_model": result.requested_model,
               "response_header_names": list(result.response_header_names),
               "session_tag": result.session_tag, "run_id": result.run_id,
               "prompt_sha256": result.prompt_sha256, "started": result.started,
               "finished": result.finished}
        (out / "error.json").write_text(json.dumps(doc, indent=2) + "\n",
                                        encoding="utf-8", newline="\n")
