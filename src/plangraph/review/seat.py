"""Run one review seat per leg and prompt part (stdlib only)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import family, gateway, prompt, qoder, verdict

__all__ = ["SeatSpec", "SeatOutcome", "run_seat", "overall"]

_BAD_FINISH = frozenset({"error", "timeout", "empty", "tool_call"})


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


@dataclass
class SeatOutcome:
    """One written seat dir with its judged verdict."""

    leg: str
    family: str
    dir: Path
    verdict: verdict.Verdict
    pass_allowed: bool
    error: str | None


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
    import json as _json

    doc = {"verdict": None, "test_gaming": "missing",
           "test_gaming_quote": None, "finish_reason": finish or "error",
           "leg": leg.name, "family": leg.family, "part": part,
           "error": error}
    (out / "evidence.json").write_text(_json.dumps(doc, indent=2) + "\n",
                                       encoding="utf-8", newline="\n")


def run_seat(spec: SeatSpec, *, gateway_call=gateway.call,
             qoder_run=qoder.run) -> list[SeatOutcome]:
    """Run each eligible leg over each prompt part; collect outcomes."""
    legs = family.pick_legs(spec.writer_family, need=spec.need,
                            down=spec.down, fleet_busy=spec.fleet_busy)
    prompts = prompt.assemble_all(spec.vectors, spec.files, spec.limit)
    multi = len(prompts) > 1
    outcomes: list[SeatOutcome] = []
    for leg in legs:
        for i, text in enumerate(prompts):
            name = "%s-p%d" % (leg.name, i + 1) if multi else leg.name
            out = Path(spec.out_root) / name
            try:
                if leg.kind == "gateway":
                    result = gateway_call(leg.model, text, title=spec.title)
                elif leg.kind == "qoder":
                    result = qoder_run(text, model=leg.model)
                else:
                    raise ValueError("unknown leg kind %r" % (leg.kind,))
            except gateway.MissingKey:
                raise
            except Exception as exc:  # noqa: BLE001 - failed leg collects
                err = "%s: %s" % (type(exc).__name__, exc) if str(exc) else type(exc).__name__
                _write_minimal(out, leg, i, "error", err)
                judged = verdict.judge("", "error", material=text)
                outcomes.append(SeatOutcome(leg=leg.name, family=leg.family,
                                            dir=out, verdict=_failed_verdict("", "error", judged),
                                            pass_allowed=False, error=err))
                continue
            body = getattr(result, "text", "") or ""
            finish = getattr(result, "finish_reason", None)
            judged = verdict.judge(body, finish, material=text)
            failed = finish in _BAD_FINISH
            error: str | None = None
            if leg.kind == "gateway" and getattr(result, "status", None) != 200:
                failed = True
                error = getattr(result, "error", None) or "status %r" % (
                    getattr(result, "status", None),)
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
            else:
                qoder.write_seat_dir(out, result, extra=extra)
            outcomes.append(SeatOutcome(leg=leg.name, family=leg.family,
                                        dir=out, verdict=judged,
                                        pass_allowed=pass_allowed, error=error))
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
