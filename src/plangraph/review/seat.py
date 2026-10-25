"""Run one review seat per leg and prompt part (stdlib only).

A leg that times out, answers HTTP 429/5xx, is rate limited, or returns an
incomplete reply is marked down and the seat retries on the next family
unlike the writer's, at most once per known leg and prompt part. No leg is required.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import family, gateway, opencode, prompt, qoder, verdict

__all__ = ["SeatSpec", "SeatOutcome", "run_seat", "overall"]

_BAD_FINISH = frozenset({"error", "timeout", "empty", "tool_call"})
_INCOMPLETE = _BAD_FINISH | {"length"}


@dataclass
class SeatSpec:
    """Inputs for one seat run (legs, prompts, output root)."""

    key: str
    writer_family: str
    vectors: list[str]
    files: dict[str, str]
    need: int = 1
    down: frozenset[str] = frozenset()
    fleet_busy: int = 0
    out_root: Path = field(default_factory=lambda: Path("."))
    title: str | None = None
    limit: int = 60_000
    diff: str | None = None  # when set, split by module instead of `files`


@dataclass
class SeatOutcome:
    """One written seat dir with its judged verdict."""

    leg: str
    family: str
    dir: Path
    verdict: verdict.Verdict
    pass_allowed: bool
    error: str | None
    tried: list[str] = field(default_factory=list)  # legs tried for this part
    part: int = 0  # 0-based prompt part


def _failed_verdict(text: str, finish: str | None,
                    judged: verdict.Verdict) -> verdict.Verdict:
    """Same judgement with the verdict word cleared (never passes)."""
    return verdict.Verdict(
        verdict=None,
        finish_reason=finish,
        test_gaming=judged.test_gaming,
        test_gaming_quote=judged.test_gaming_quote,
        empty=(text or "").strip() == "",
    )


def _write_minimal(out: Path, leg: family.Leg, part: int,
                   finish: str | None, error: str) -> None:
    """Empty review.txt plus a minimal evidence.json for a raised leg."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.txt").write_text("", encoding="utf-8", newline="\n")
    doc = {"verdict": None, "test_gaming": "missing",
           "test_gaming_quote": None, "finish_reason": finish or "error",
           "leg": leg.name, "family": leg.family, "part": part,
           "error": error}
    (out / "evidence.json").write_text(json.dumps(doc, indent=2) + "\n",
                                       encoding="utf-8", newline="\n")


def _retriable(result, leg: family.Leg) -> bool:
    """True when the leg is unusable now: 429/5xx, timeout, incomplete."""
    finish = getattr(result, "finish_reason", None)
    if getattr(result, "rate_limited", False):
        return True
    if leg.kind == "gateway":
        status = getattr(result, "status", None)
        if status != 200:
            if not isinstance(status, int) or isinstance(status, bool):
                return True          # unknown/garbled status: retriable-unknown, never a TypeError
            return status in (0, 429) or status >= 500
    if finish in _INCOMPLETE:
        return True
    return not (getattr(result, "text", "") or "").strip()


def _run_part(leg: family.Leg, i: int, text: str, multi: bool, spec: SeatSpec,
              calls: dict) -> tuple[SeatOutcome, bool]:
    """Run one prompt part on one leg; return its outcome and a retry flag."""
    name = "%s-p%d" % (leg.name, i + 1) if multi else leg.name
    out = Path(spec.out_root) / name
    try:
        if leg.kind == "gateway":
            result = calls["gateway"](leg.model, text, title=spec.title)
        elif leg.kind == "qoder":
            result = calls["qoder"](text, model=leg.model)
        elif leg.kind == "opencode":
            result = calls["opencode"](text, model=leg.model)
        else:
            raise ValueError("unknown leg kind %r" % (leg.kind,))
    except gateway.MissingKey:
        raise
    except Exception as exc:  # noqa: BLE001 - failed leg collects
        err = "%s: %s" % (type(exc).__name__, exc) if str(exc) else type(exc).__name__
        _write_minimal(out, leg, i, "error", err)
        judged = verdict.judge("", "error", material=text)
        return SeatOutcome(leg=leg.name, family=leg.family, dir=out,
                           verdict=_failed_verdict("", "error", judged),
                           pass_allowed=False, error=err, part=i), True
    body = getattr(result, "text", "") or ""
    finish = getattr(result, "finish_reason", None)
    judged = verdict.judge(body, finish, material=text)
    failed = finish in _BAD_FINISH
    error: str | None = None
    truncated = getattr(result, "truncated_empty", False)
    status = getattr(result, "status", None)
    if leg.kind == "gateway" and (status != 200 or truncated):
        failed = True  # truncated_empty is a failed seat, never a verdict
        error = getattr(result, "error", None) or (
            "truncated_empty" if truncated else "status %r" % (status,))
    retry = _retriable(result, leg)
    if failed:
        error = error or getattr(result, "error", None) or "finish_reason %r" % (finish,)
        judged = _failed_verdict(body, finish, judged)
        pass_allowed = False
    else:
        pass_allowed = judged.pass_allowed
    extra = judged.to_evidence()
    extra.update({"leg": leg.name, "family": leg.family, "part": i})
    if leg.kind == "gateway":
        gateway.write_seat_dir(out, result, extra=extra)
    elif leg.kind == "qoder":
        qoder.write_seat_dir(out, result, extra=extra)
    else:
        opencode.write_seat_dir(out, result, extra=extra)
    return SeatOutcome(leg=leg.name, family=leg.family, dir=out, verdict=judged,
                       pass_allowed=pass_allowed, error=error, part=i), retry


def _skipped(leg: family.Leg, i: int, multi: bool, spec: SeatSpec, text: str) -> SeatOutcome:
    """Incomplete outcome for a part whose seat leg is down and has no replacement."""
    name = "%s-p%d" % (leg.name, i + 1) if multi else leg.name
    out = Path(spec.out_root) / name
    err = "leg %s is down and no eligible replacement" % leg.name
    _write_minimal(out, leg, i, "error", err)
    judged = verdict.judge("", "error", material=text)
    return SeatOutcome(leg=leg.name, family=leg.family, dir=out,
                       verdict=_failed_verdict("", "error", judged),
                       pass_allowed=False, error=err, tried=[], part=i)


def run_seat(spec: SeatSpec, *, gateway_call=gateway.call,
             qoder_run=qoder.run, opencode_run=opencode.run) -> list[SeatOutcome]:
    """Run each eligible leg over each prompt part; retry down legs per part.

    Each of `need` seats starts on its picked leg. A part whose leg is
    retriable-failed (timeout, 429/5xx, rate limit, incomplete reply) marks
    that leg down and is retried on the next family unlike the writer's, for
    that part only: parts that already completed are never re-run, and the
    next part starts on the seat's own leg unless that leg is now down.
    Every attempt is returned; `tried` lists the legs tried for that part.
    """
    first = family.pick_legs(spec.writer_family, need=spec.need,
                             down=spec.down, fleet_busy=spec.fleet_busy)
    if spec.diff is not None:
        prompts = prompt.assemble_diff(spec.vectors, spec.diff, spec.limit)
    else:
        prompts = prompt.assemble_all(spec.vectors, spec.files, spec.limit)
    calls = {"gateway": gateway_call, "qoder": qoder_run,
             "opencode": opencode_run}
    down = set(spec.down)
    claimed = [{leg.name} for leg in first]  # legs each seat holds; others stay off them
    multi = len(prompts) > 1
    outcomes: list[SeatOutcome] = []
    for seat_no, seat_leg in enumerate(first):
        for i, text in enumerate(prompts):
            leg, tried, attempts = seat_leg, [], []
            if leg.name in down:  # went down on an earlier part: move on at once
                blocked = down | {n for k, c in enumerate(claimed) if k != seat_no for n in c}
                try:
                    leg = family.pick_legs(spec.writer_family, need=1,
                                           down=frozenset(blocked))[0]
                except family.NoEligibleLeg:
                    # Never re-run a leg already marked down: this part fails (incomplete).
                    outcomes.append(_skipped(seat_leg, i, multi, spec, text))
                    continue
                claimed[seat_no].add(leg.name)
            for _ in range(len(family.LEGS)):
                tried.append(leg.name)
                item, retry = _run_part(leg, i, text, multi, spec, calls)
                attempts.append(item)
                if not retry:
                    break
                down.add(leg.name)
                blocked = down | {n for k, c in enumerate(claimed) if k != seat_no for n in c}
                try:
                    leg = family.pick_legs(spec.writer_family, need=1,
                                           down=frozenset(blocked))[0]
                except family.NoEligibleLeg:
                    break
                claimed[seat_no].add(leg.name)
            for item in attempts:
                item.tried = list(tried)
            outcomes.extend(attempts)
    return outcomes


def overall(outcomes: list[SeatOutcome]) -> dict[str, str]:
    """Leg name to pass, fix, halt, or incomplete across its parts."""
    grouped: dict[str, list[SeatOutcome]] = {}
    for item in outcomes:
        grouped.setdefault(item.leg, []).append(item)
    result: dict[str, str] = {}
    for leg, parts in grouped.items():
        words = [p.verdict.verdict if isinstance(p.verdict, verdict.Verdict) else p.verdict
                 for p in parts]
        if any(w == "halt" for w in words):
            result[leg] = "halt"
        elif any(w == "fix" for w in words):
            result[leg] = "fix"
        elif parts and all(p.pass_allowed for p in parts):
            result[leg] = "pass"
        else:
            result[leg] = "incomplete"
    return result
