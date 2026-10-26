"""Tool-less review leg that shells out to the qoder CLI (stdlib only).

The command shape is fixed (argv list, no shell)::

    timeout <secs> qodercli -p --model <model> --tools "" \
        --no-session-persistence -o json <prompt>

run with cwd set to a fresh empty temp dir. The leading preamble
("You have NO tools...") is the caller's job, not this module's.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import verdict

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "RATE_LIMIT_PATTERN",
    "QoderResult",
    "argv",
    "parse_output",
    "classify",
    "is_rate_limited",
    "run",
    "write_seat_dir",
]

DEFAULT_MODEL = "Qwen3.8-Flash"
DEFAULT_TIMEOUT = 900
# A bare "429" is a port or a count; only a rate-limit phrase or an HTTP/status 429 counts.
RATE_LIMIT_PATTERN = (r"rate[ _-]?limit|too many requests"
                      r"|\bhttp(?:/\d(?:\.\d)?)?[ /_-]*(?:status[ :=]*)?429\b|\bstatus(?: code)?[ :=]+429\b"
                      r"|\b429[ :-]+too many")
_RATE_LIMIT = re.compile(RATE_LIMIT_PATTERN, re.I)


def is_rate_limited(text: str | None) -> bool:
    """True when stderr text names a rate limit or an HTTP/status 429 (not a bare 429)."""
    return bool(_RATE_LIMIT.search(text or ""))


def _utc_now() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def argv(prompt: str, model: str = DEFAULT_MODEL, binary: str = "qodercli") -> list[str]:
    """CLI argv for one call (no shell, no timeout wrapper)."""
    return [binary, "-p", "--model", model, "--tools", "",
            "--no-session-persistence", "-o", "json", prompt]


def _result_object(stdout: str) -> dict | None:
    """Best-effort ``{"type": "result"}`` object from stdout, or None."""
    try:
        payload: Any = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, list):
        for item in reversed(payload):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
        return None
    if isinstance(payload, dict):
        if payload.get("type") == "result" or "result" in payload:
            return payload
    return None


def parse_output(stdout: str) -> dict:
    """Parse ``-o json`` stdout into text/is_error/num_turns/usage/subtype."""
    obj = _result_object(stdout or "")
    if obj is None:
        return {"text": "", "is_error": True, "num_turns": None,
                "usage": {}, "subtype": None}
    result = obj.get("result")
    text = result if isinstance(result, str) else ""
    is_error = bool(obj.get("is_error", False))
    num_turns = obj.get("num_turns")
    if not isinstance(num_turns, int) or isinstance(num_turns, bool):
        num_turns = None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    subtype = obj.get("subtype")
    if not isinstance(subtype, str):
        subtype = None
    return {"text": text, "is_error": is_error, "num_turns": num_turns,
            "usage": usage, "subtype": subtype}


def _leading_json_object(text: str) -> dict | None:
    """First JSON value when text starts with ``{``, else None."""
    stripped = (text or "").lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(stripped)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def classify(parsed: dict) -> str:
    """Finish reason for a parsed object (error/empty/tool_call/length/stop)."""
    if parsed.get("is_error"):
        return "error"
    text = parsed.get("text") or ""
    if not text.strip():
        return "empty"
    num_turns = parsed.get("num_turns")
    if "<tool_call>" in text or '"tool_calls"' in text or "<function=" in text:
        return "tool_call"
    leading = _leading_json_object(text)
    if isinstance(leading, dict) and "name" in leading and "arguments" in leading:
        return "tool_call"
    if isinstance(num_turns, int) and not isinstance(num_turns, bool) and num_turns > 1:
        return "tool_call"
    subtype = parsed.get("subtype") or ""
    lowered = subtype.lower() if isinstance(subtype, str) else ""
    if "max" in lowered or "length" in lowered:
        return "length"
    return "stop"


@dataclass
class QoderResult:
    """One finished qoder CLI call."""

    rc: int | None
    text: str
    finish_reason: str
    num_turns: int | None
    is_error: bool
    model: str
    prompt_sha256: str
    started: str
    finished: str
    error: str | None
    usage: dict | None = field(default=None)
    rate_limited: bool = False

    def to_evidence(self) -> dict:
        """Evidence fields for this leg (tokens only when usage present)."""
        doc: dict = {
            "requested_model": self.model,
            "served_model": self.model,
            "status": self.rc,
            "finish_reason": self.finish_reason,
            "prompt_sha256": self.prompt_sha256,
            "started": self.started,
            "finished": self.finished,
            "leg": "qoder",
        }
        if isinstance(self.usage, dict) and self.usage:
            doc["tokens"] = dict(self.usage)
        return doc


def run(prompt: str, *, model: str = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT,
        binary: str = "qodercli",
        runner: Callable = subprocess.run) -> QoderResult:
    """Run the CLI in a fresh empty temp dir and parse its stdout."""
    sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    started = _utc_now()
    cmd = ["timeout", str(timeout)] + argv(prompt, model=model, binary=binary)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            completed = runner(cmd, cwd=tmp, capture_output=True,
                               text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return QoderResult(rc=None, text="", finish_reason="timeout",
                           num_turns=None, is_error=True, model=model,
                           prompt_sha256=sha, started=started,
                           finished=_utc_now(), error=str(exc) or "timeout")
    except FileNotFoundError:
        return QoderResult(rc=None, text="", finish_reason="error",
                           num_turns=None, is_error=True, model=model,
                           prompt_sha256=sha, started=started,
                           finished=_utc_now(), error="binary not found")
    except OSError as exc:
        return QoderResult(rc=None, text="", finish_reason="error",
                           num_turns=None, is_error=True, model=model,
                           prompt_sha256=sha, started=started,
                           finished=_utc_now(), error=str(exc))
    stdout = getattr(completed, "stdout", "") or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    parsed = parse_output(stdout)
    stderr = getattr(completed, "stderr", "") or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    finish = classify(parsed)
    limited = is_rate_limited(stderr) and (
        finish != "stop" or getattr(completed, "returncode", 0) not in (0, None))
    rc = getattr(completed, "returncode", None)
    error = "rate limit" if limited else None
    if rc not in (0, None) and not limited:
        # A non-zero exit means the reply may be partial or corrupt: incomplete, retriable.
        finish, error = "error", "exit %s" % rc
    usage = parsed.get("usage") or None
    if isinstance(usage, dict) and not usage:
        usage = None
    return QoderResult(rc=rc,
                       text=parsed["text"],
                       finish_reason=finish,
                       num_turns=parsed["num_turns"],
                       is_error=parsed["is_error"], model=model,
                       prompt_sha256=sha, started=started,
                       finished=_utc_now(),
                       error=error,
                       usage=usage, rate_limited=limited)


def write_seat_dir(out_dir: Path, result: QoderResult,
                   extra: dict | None = None,
                   material: str = "") -> None:
    """Write ``review.txt`` + ``evidence.json`` (no raw stdout kept).

    The parsed verdict fields always come from ``verdict.judge`` over the
    reply and ``material`` (the diff/context the seat saw), so a caller that
    passes no ``extra`` still leaves a seat dir tools/seat_verdict.py can
    gate on.  ``extra`` is applied last but never overwrites judge fields,
    except that it may make them stricter (a caller's failed run clears the
    verdict; a caller's gaming finding stands).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.txt").write_text(result.text or "", encoding="utf-8")
    evidence = result.to_evidence()
    judged = verdict.judge(result.text or "",
                           evidence.get("finish_reason"),
                           material).to_evidence()
    evidence.update(judged)
    if extra:
        evidence.update({k: v for k, v in extra.items() if k not in judged})
        if "verdict" in extra and extra["verdict"] != "pass":
            evidence["verdict"] = extra["verdict"]
        if extra.get("test_gaming") not in (None, "no"):
            evidence["test_gaming"] = extra["test_gaming"]
    (out / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n",
                                       encoding="utf-8")
