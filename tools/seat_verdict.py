#!/usr/bin/env python3
"""Record one review seat's verdict in the keeper (stdlib only).

A review seat is a tool-less LLM call that leaves a directory holding
``review.txt`` (the answer) and ``evidence.json`` (served_model, finish_reason,
verdict, test_gaming, correlation_id, prompt_sha256, ...).  This tool parses the
verdict strictly, checks the reviewing model's family, and POSTs
``/v1/verdict`` with ``Authorization: Bearer <reviewer-<family>.token>``.

The JSON ``ok`` is always a real boolean: the keeper coerces it with ``bool()``,
so a string such as ``"false"`` would be read as true.

Exit codes: 0 sent (2xx), 1 I/O or keeper error, 2 refused (unknown model
family, empty writer-family list, a Claude seat over a writer list that is
unknown or contains anthropic, or a seat whose family covers no listed writer),
3 no parseable verdict, 4 pass refused.  A Claude (anthropic) seat is accepted
only over a known non-anthropic writer list.  coverage() computes which writer
families the recorded seats cover.  A PASS is sent only when finish_reason is
"stop" or "end_turn" AND test_gaming is "no" (case-insensitive); missing, "unsure", "yes" or
anything else refuses it.  fix/halt verdicts are sent regardless of test_gaming.  The token is read at send time and never printed.
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

ROUTE = "/v1/verdict"
TIMEOUT = 30
EXIT_IO, EXIT_REFUSED, EXIT_NO_VERDICT, EXIT_PASS_REFUSED = 1, 2, 3, 4

# (regex on the lowercased model name without any "vendor/" prefix, family)
FAMILIES = (
    (r"gpt|^o\d", "openai"), (r"mimo", "xiaomi"), (r"llama|muse", "meta"),
    (r"deepseek", "deepseek"), (r"qwen", "alibaba"), (r"gemini", "google"),
    (r"claude|sonnet|opus|haiku", "anthropic"),
    (r"glm|zhipu", "zhipu"),
    (r"nemotron", "nvidia"),
)
KNOWN_FAMILIES = frozenset(family for _, family in FAMILIES)
PASS_WORDS = frozenset({"pass", "approve"})
FIX_WORDS = frozenset({"fix", "halt", "change", "changes", "reject"})
_VERDICT_LINE = re.compile(r"^[\s>#*_`-]*verdict[\s*_`]*:[\s*_`]*(.*)$", re.I)
# first word, ended by end/space/punctuation and NOT followed by "|" or "/"
# (that would be an echoed "pass | fix | halt" template, not a decision)
_FIRST_WORD = re.compile(r"([a-z]+)(?=$|[\s.,;:!)(*_`—–-])(?!\s*[|/])", re.I)


def model_family(model):
    """Family name for a model id, or None when unknown or ambiguous."""
    name = str(model or "").lower().rsplit("/", 1)[-1]
    # D-1714: qodercli's Efficient alias serves the qwen3.8-flash Alibaba seat.
    if name == "efficient":
        return "alibaba"
    found = {family for pattern, family in FAMILIES if re.search(pattern, name)}
    return found.pop() if len(found) == 1 else None


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


def build_reason(evidence, ok, text, idx, covers=None):
    head = (f"seat {evidence.get('served_model') or evidence.get('requested_model')}"
            f" PASS corr {evidence.get('correlation_id')}"
            f" prompt {str(evidence.get('prompt_sha256') or '')[:12]}")
    reason = head if ok else f"{head}\n{findings(text, idx)}".rstrip()
    if covers is not None:
        reason = f"{reason}\ncovers: {', '.join(covers)}"
    return reason


def coverage(writers, seats, need=2, sensitive=False):
    """Return sorted writer families not covered by the recorded seats.

    All anthropic seats count as one family.  A writer is covered when at least
    ``need`` distinct other seat families exist; with ``sensitive`` there must
    also be a non-anthropic seat family different from the writer.
    """
    writer_set = sorted({str(w).strip().lower() for w in writers if str(w).strip()})
    seat_families = {str(seat).strip().lower() for seat in seats if str(seat).strip()}
    uncovered = []
    for writer in writer_set:
        others = {family for family in seat_families if family != writer}
        covered = len(others) >= need
        if covered and sensitive:
            covered = any(family != "anthropic" and family != writer
                          for family in seat_families)
        if not covered:
            uncovered.append(writer)
    return sorted(uncovered)


def parse_writer_families(value):
    """Parse comma-separated writer families, ignoring empty entries."""
    return [part.strip().lower() for part in str(value or "").split(",") if part.strip()]


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
    ap.add_argument("--url", default=os.environ.get("PG_URL"))
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
    writer_families = parse_writer_families(args.writer_family)
    if not writer_families:
        print("seat_verdict: refused, writer family list is empty", file=sys.stderr)
        return EXIT_REFUSED
    unknown_writers = [w for w in writer_families if w not in KNOWN_FAMILIES]
    if unknown_writers:
        print(f"seat_verdict: refused, unknown writer families "
              f"{unknown_writers!r}", file=sys.stderr)
        return EXIT_REFUSED
    model = evidence.get("served_model") or evidence.get("requested_model")
    family = model_family(model)
    if family is None:
        print(f"seat_verdict: refused, family {family!r} of model {model!r} "
              f"is unknown", file=sys.stderr)
        return EXIT_REFUSED
    if family == "anthropic":
        if "anthropic" in writer_families:
            print(f"seat_verdict: refused, anthropic seat over writer families "
                  f"{writer_families!r}", file=sys.stderr)
            return EXIT_REFUSED
    elif all(writer == family for writer in writer_families):
        print(f"seat_verdict: refused, family {family!r} of model {model!r} "
              f"covers no listed writer", file=sys.stderr)
        return EXIT_REFUSED
    ok, idx = parse_verdict(text)
    if ok is None:
        print("seat_verdict: no parseable VERDICT line", file=sys.stderr)
        return EXIT_NO_VERDICT
    gaming = evidence.get("test_gaming")
    if ok and (evidence.get("finish_reason") not in ("stop", "end_turn") or not isinstance(gaming, str)
               or gaming.strip().lower() != "no"):
        print("seat_verdict: pass refused (finish_reason not stop or end_turn, or test_gaming not no)",
              file=sys.stderr)
        return EXIT_PASS_REFUSED
    covers = sorted({writer for writer in writer_families if writer != family})
    payload = verdict_payload(args.key, ok,
                              build_reason(evidence, ok, text, idx, covers))
    if args.dry_run:
        print(json.dumps({"url": (args.url or "").rstrip("/") + ROUTE, "family": family,
                          "headers": {"Authorization": "Bearer <redacted>"},
                          "body": payload}, indent=2))
        return 0
    if not args.url:
        print("seat_verdict: no keeper URL: pass --url or set PG_URL", file=sys.stderr)
        return EXIT_IO
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
