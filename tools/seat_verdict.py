#!/usr/bin/env python3
"""Record one review seat's verdict in the keeper (stdlib only).

A review seat is a tool-less LLM call that leaves a directory holding
``review.txt`` (the answer) and ``evidence.json`` (served_model, finish_reason,
verdict, test_gaming, correlation_id, prompt_sha256, ...).  This tool parses the
verdict strictly, checks the reviewing model's family, and POSTs
``/v1/verdict`` with ``Authorization: Bearer <reviewer-<family>.token>``.

The JSON ``ok`` is always a real boolean: the keeper coerces it with ``bool()``,
so a string such as ``"false"`` would be read as true.

Exit codes: 0 sent (2xx), 1 I/O or keeper error, 2 refused (unknown, writer or
anthropic family), 3 no parseable verdict, 4 pass refused (finish_reason or
test gaming).  The token is read at send time and never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8787"
ROUTE = "/v1/verdict"
TIMEOUT = 30
EXIT_IO, EXIT_REFUSED, EXIT_NO_VERDICT, EXIT_PASS_REFUSED = 1, 2, 3, 4

# (regex on the lowercased model name without any "vendor/" prefix, family)
FAMILIES = (
    (r"gpt|^o\d", "openai"), (r"mimo", "xiaomi"), (r"llama|muse", "meta"),
    (r"deepseek", "deepseek"), (r"qwen", "alibaba"), (r"gemini", "google"),
    (r"claude|sonnet|opus|haiku", "anthropic"),
)
PASS_WORDS = frozenset({"pass", "approve"})
FIX_WORDS = frozenset({"fix", "halt", "change", "changes", "reject"})
_VERDICT_LINE = re.compile(r"^[\s>#*_`-]*verdict[\s*_`]*:[\s*_`]*(.*)$", re.I)
# first word, ended by end/space/punctuation and NOT followed by "|" or "/"
# (that would be an echoed "pass | fix | halt" template, not a decision)
_FIRST_WORD = re.compile(r"([a-z]+)(?=$|[\s.,;:!)(*_`—–-])(?!\s*[|/])", re.I)


def model_family(model):
    """Family name for a model id, or None when unknown."""
    name = str(model or "").lower().rsplit("/", 1)[-1]
    for pattern, family in FAMILIES:
        if re.search(pattern, name):
            return family
    return None


def parse_verdict(text):
    """Return (ok, index_of_line) from the LAST ``VERDICT:`` line, else (None, -1).

    Only that line decides.  A template echo such as ``VERDICT: pass | fix`` or
    an unknown first word yields no verdict; earlier lines are never consulted.
    """
    lines = text.splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        match = _VERDICT_LINE.match(lines[idx])
        if not match:
            continue
        word = _FIRST_WORD.match(match.group(1).strip())
        word = word.group(1).lower() if word else ""
        if word in PASS_WORDS:
            return True, idx
        if word in FIX_WORDS:
            return False, idx
        return None, idx
    return None, -1


def findings(text, idx, limit=600):
    """First ``limit`` chars after the verdict line (before it if nothing after)."""
    lines = text.splitlines()
    after = "\n".join(lines[idx + 1:]).strip()
    return (after or "\n".join(lines[:idx]).strip())[:limit]


def verdict_payload(key, ok, reason):
    """The exact JSON body the keeper's verdict route takes."""
    assert type(ok) is bool, "ok must be a JSON boolean, never a string"
    return {"key": str(key), "ok": ok, "reason": str(reason)}


def build_reason(evidence, ok, text, idx):
    head = (f"seat {evidence.get('served_model') or evidence.get('requested_model')}"
            f" PASS corr {evidence.get('correlation_id')}"
            f" prompt {str(evidence.get('prompt_sha256') or '')[:12]}")
    return head if ok else f"{head}\n{findings(text, idx)}".rstrip()


def send(url, token, payload):
    """POST the payload; return (http_status, reply_text)."""
    request = urllib.request.Request(
        url.rstrip("/") + ROUTE, data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json",
                                "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536).decode("utf-8", "replace")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--key", required=True, help="item key to review")
    ap.add_argument("--seat-dir", required=True, type=Path)
    ap.add_argument("--writer-family", required=True)
    ap.add_argument("--token-dir", required=True, type=Path)
    ap.add_argument("--url", default=os.environ.get("PG_URL") or DEFAULT_URL)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        evidence = json.loads((args.seat_dir / "evidence.json").read_text("utf-8"))
        text = (args.seat_dir / "review.txt").read_text("utf-8")
    except (OSError, ValueError) as exc:
        print(f"seat_verdict: cannot read seat dir: {type(exc).__name__}", file=sys.stderr)
        return EXIT_IO
    if not isinstance(evidence, dict):
        print("seat_verdict: evidence.json is not an object", file=sys.stderr)
        return EXIT_IO
    model = evidence.get("served_model") or evidence.get("requested_model")
    family = model_family(model)
    if family is None or family == "anthropic" or family == args.writer_family.lower():
        print(f"seat_verdict: refused, family {family!r} of model {model!r} "
              f"is unknown, anthropic or the writer's", file=sys.stderr)
        return EXIT_REFUSED
    ok, idx = parse_verdict(text)
    if ok is None:
        print("seat_verdict: no parseable VERDICT line", file=sys.stderr)
        return EXIT_NO_VERDICT
    if ok and (evidence.get("finish_reason") != "stop"
               or str(evidence.get("test_gaming")).lower() == "yes"):
        print("seat_verdict: pass refused (finish_reason not stop or test gaming)",
              file=sys.stderr)
        return EXIT_PASS_REFUSED
    payload = verdict_payload(args.key, ok, build_reason(evidence, ok, text, idx))
    if args.dry_run:
        print(json.dumps({"url": args.url.rstrip("/") + ROUTE, "family": family,
                          "headers": {"Authorization": "Bearer <redacted>"},
                          "body": payload}, indent=2))
        return 0
    try:
        token = (args.token_dir / f"reviewer-{family}.token").read_text("utf-8").strip()
        status, reply = send(args.url, token, payload)
    except (OSError, ValueError) as exc:
        print(f"seat_verdict: send failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_IO
    short = " ".join(reply.split())[:120]
    print(f"key={args.key} family={family} ok={ok} http={status} reply={short}")
    return 0 if 200 <= status < 300 else EXIT_IO


if __name__ == "__main__":
    sys.exit(main())
