"""Review leg that shells out to the opencode CLI (stdlib only).

Command shape (argv list, no shell)::

    opencode run -m <model> [--attach <server-url>] -- <prompt>

``--attach`` is added only when ``PLANGRAPH_OPENCODE_SERVER_URL`` is set;
there is no default host. Runs in a fresh empty temp dir. This is the
fallback leg when Claude or the other legs are rate limited (D-1533).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .qoder import RATE_LIMIT_PATTERN

__all__ = ["SERVER_ENV_VAR", "DEFAULT_MODEL", "DEFAULT_TIMEOUT",
           "OpencodeResult", "argv", "is_rate_limited", "run",
           "write_seat_dir"]

SERVER_ENV_VAR = "PLANGRAPH_OPENCODE_SERVER_URL"
DEFAULT_MODEL = "opencode/deepseek-v3"
DEFAULT_TIMEOUT = 900
_RATE_LIMIT = re.compile(RATE_LIMIT_PATTERN + r"|usage limit", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _utc_now() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_rate_limited(text: str | None) -> bool:
    """True when text names a rate limit or an HTTP/status 429 (not a bare 429)."""
    return bool(_RATE_LIMIT.search(text or ""))


def check_inputs(model: str, server_url: str | None) -> None:
    """Reject a model/url that would parse as a flag, a url with userinfo, or a non-http(s) scheme."""
    if not isinstance(model, str) or not model.strip() or model.lstrip().startswith("-"):
        raise ValueError("unsafe opencode model %r" % (model,))
    if server_url:
        if server_url.lstrip().startswith("-"):
            raise ValueError("unsafe opencode server url (starts with '-')")
        parts = urlsplit(server_url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("opencode server url must be http(s) with a host")
        if "@" in parts.netloc or parts.username or parts.password:
            raise ValueError("opencode server url must not carry userinfo")


def argv(prompt: str, model: str = DEFAULT_MODEL, server_url: str | None = None,
         binary: str = "opencode") -> list[str]:
    """CLI argv for one call (no shell); ValueError for an unsafe model or server url."""
    check_inputs(model, server_url)
    cmd = [binary, "run", "-m", model]
    if server_url:
        cmd += ["--attach", server_url]
    return cmd + ["--", prompt]  # the prompt is never parsed as a flag


@dataclass
class OpencodeResult:
    """One finished opencode CLI call."""

    rc: int | None
    text: str
    finish_reason: str
    model: str
    prompt_sha256: str
    started: str
    finished: str
    error: str | None
    rate_limited: bool = False
    stderr: str = field(default="")

    def to_evidence(self) -> dict:
        """Evidence fields for this leg."""
        return {"requested_model": self.model, "served_model": self.model,
                "status": self.rc, "finish_reason": self.finish_reason,
                "prompt_sha256": self.prompt_sha256, "started": self.started,
                "finished": self.finished, "leg": "opencode",
                "rate_limited": self.rate_limited}


def run(prompt: str, *, model: str = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT,
        binary: str = "opencode", env: Any | None = None,
        runner: Callable = subprocess.run) -> OpencodeResult:
    """Run the CLI in a fresh empty temp dir; plain stdout is the reply."""
    source = env if env is not None else os.environ
    server = (source.get(SERVER_ENV_VAR) or "").strip()
    sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    started = _utc_now()
    def failed(reason: str, error: str, rc: int | None = None,
               stderr: str = "") -> OpencodeResult:
        return OpencodeResult(rc=rc, text="", finish_reason=reason, model=model,
                              prompt_sha256=sha, started=started,
                              finished=_utc_now(), error=error, stderr=stderr)

    try:
        cmd = argv(prompt, model=model, server_url=server or None, binary=binary)
    except ValueError as exc:
        return failed("error", str(exc))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            done = runner(cmd, cwd=tmp, capture_output=True, text=True,
                          timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return failed("timeout", str(exc) or "timeout")
    except FileNotFoundError:
        return failed("error", "binary not found")
    except OSError as exc:
        return failed("error", str(exc))
    out, err = (getattr(done, "stdout", "") or ""), (getattr(done, "stderr", "") or "")
    out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
    err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
    out, err = _ANSI.sub("", out), _ANSI.sub("", err)
    rc = getattr(done, "returncode", None)
    failed_rc = rc not in (0, None)  # a 429 in the output of a clean exit is just text
    limited = failed_rc and (is_rate_limited(err) or is_rate_limited(out))
    if limited:
        res = failed("error", "rate limit", rc, err[:200])
        res.rate_limited = True
        return res
    if rc not in (0, None):
        return failed("error", "exit %s" % rc, rc, err[:200])
    if not out.strip():
        return failed("empty", "empty reply", rc, err[:200])
    return OpencodeResult(rc=rc, text=out, finish_reason="stop", model=model,
                          prompt_sha256=sha, started=started,
                          finished=_utc_now(), error=None, stderr=err[:200])


def write_seat_dir(out_dir: Path, result: OpencodeResult,
                   extra: dict | None = None) -> None:
    """Write ``review.txt`` + ``evidence.json``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.txt").write_text(result.text or "", encoding="utf-8")
    evidence = result.to_evidence()
    if extra:
        evidence.update(extra)
    (out / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n",
                                       encoding="utf-8")
