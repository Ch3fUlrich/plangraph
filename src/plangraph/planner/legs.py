"""Routing legs: the ordered model/harness combos the keeper may hand a card to (D25).

A *leg* is one concrete way to run a card: a harness, a model, the window classes it
serves and the largest request it accepts.  ``DEFAULT_LEGS`` lists them in the D25
preference order (position = preference).  The numbers are defaults; the measured
windows kept in the registry override them via :func:`with_measured`.  An operator
file (``{"legs": [...]}``) replaces the whole list via :func:`load_legs`.

Tiers run cheapest first: ``trial``, ``free``, ``subscription``, ``paid``.  The trial
tier is empty by default.  Pure data and validation; the only I/O is reading the
optional JSON override file.
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from plangraph.keeper.schema import WINDOW_CLASSES
from plangraph.mimo_policy import MimoPathError, check_mimo_path

__all__ = ["TIERS", "HARNESSES", "Leg", "DEFAULT_LEGS", "load_legs", "with_measured"]

#: Cost tiers, cheapest first.
TIERS = ("trial", "free", "subscription", "paid")
#: Harness names a leg may run under.
HARNESSES = ("qodercli", "opencode", "agy")


@dataclass(frozen=True)
class Leg:
    """One routing leg (see module docstring)."""

    id: str
    tier: str
    harness: str
    model: str
    family: str
    windows: tuple[str, ...]
    max_request_tokens: int
    trains_on_data: bool
    claude_family: bool = False
    note: str = ""


def _leg(id, tier, harness, model, family, windows, max_req, trains, claude=False,
         note=""):
    return Leg(id, tier, harness, model, family, tuple(windows), max_req, trains,
               claude, note)


#: D25 order.  Position is the preference inside a tier.
DEFAULT_LEGS: tuple[Leg, ...] = (
    _leg("qodercli-qwen3.8-flash", "free", "qodercli", "Qwen3.8-Flash", "qwen",
         ("128k", "256k"), 100000, False,
         note="free while qoder credits remain, until its rate limit"),
    _leg("opencode-zen-free", "free", "opencode",
         "opencode/muse-spark-1.3-contributor-free", "muse", ("128k",), 100000, True),
    _leg("or-north-mini-code-free", "free", "opencode",
         "omniroute/or-north-mini-code-free", "north", ("128k",), 90000, True),
    _leg("or-laguna-s-2.1-free", "free", "opencode",
         "omniroute/or-laguna-s-2.1-free", "laguna", ("128k",), 90000, True),
    _leg("or-nemotron-3-super-free", "free", "opencode",
         "omniroute/or-nemotron-3-super-free", "nvidia", ("128k", "256k"), 100000, True),
    _leg("ovh-gpt-oss-120b", "free", "opencode", "omniroute/ovh-gpt-oss-120b", "openai",
         ("128k",), 90000, False),
    _leg("mimo-v2.6-flash", "subscription", "opencode",
         "mimo/mimo-v2.6-flash", "mimo", ("128k", "256k", "1m"), 200000, False),
    _leg("agy-gemini-3.8-flash", "subscription", "agy", "gemini-3.8-flash", "gemini",
         ("128k", "256k", "1m"), 900000, False,
         note="gemini after its Friday-evening reset"),
    _leg("agy-sonnet-5.5", "subscription", "agy", "sonnet-5.5", "claude",
         ("128k", "256k", "1m"), 180000, False, claude=True,
         note="Antigravity quota, not Claude Max"),
    _leg("deepseek", "paid", "opencode", "omniroute/deepseek", "deepseek",
         ("128k", "256k"), 100000, False, note="pay-as-you-go, only if really needed"),
)

_FIELDS = tuple(f.name for f in dataclasses.fields(Leg))
_REQUIRED = ("id", "tier", "harness", "model", "family", "windows",
             "max_request_tokens", "trains_on_data")
_STR_FIELDS = ("id", "tier", "harness", "model", "family", "note")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _build_leg(index: int, raw, problems: list[str]) -> Leg | None:
    where = f"legs[{index}]"
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be an object")
        return None
    start = len(problems)
    if isinstance(raw.get("model"), str):
        try:
            check_mimo_path(raw["model"])
        except MimoPathError as exc:
            problems.append(f"{where}.model: {exc}")
    for key in raw:
        if key not in _FIELDS:
            problems.append(f"{where}: unknown field {key!r}")
    for key in _REQUIRED:
        if key not in raw:
            problems.append(f"{where}: missing field {key!r}")
    for key in _STR_FIELDS:
        if key in raw and not isinstance(raw[key], str):
            problems.append(f"{where}.{key}: must be a string")
    for key in ("id", "model", "family"):
        if isinstance(raw.get(key), str) and not raw[key]:
            problems.append(f"{where}.{key}: must not be empty")
    if isinstance(raw.get("tier"), str) and raw["tier"] not in TIERS:
        problems.append(f"{where}.tier: {raw['tier']!r} not in {TIERS}")
    if isinstance(raw.get("harness"), str) and raw["harness"] not in HARNESSES:
        problems.append(f"{where}.harness: {raw['harness']!r} not in {HARNESSES}")
    windows = raw.get("windows")
    if "windows" in raw:
        if (not isinstance(windows, (list, tuple)) or not windows
                or any(not isinstance(w, str) or w not in WINDOW_CLASSES
                       for w in windows)):
            problems.append(f"{where}.windows: must be a non-empty subset of "
                            f"{WINDOW_CLASSES}")
    if "max_request_tokens" in raw:
        mrt = raw["max_request_tokens"]
        if not _is_int(mrt) or mrt <= 0:
            problems.append(f"{where}.max_request_tokens: must be a positive int")
    for key in ("trains_on_data", "claude_family"):
        if key in raw and not isinstance(raw[key], bool):
            problems.append(f"{where}.{key}: must be a bool")
    if len(problems) > start:
        return None
    data = dict(raw)
    data["windows"] = tuple(windows)
    return Leg(**data)


def load_legs(path: Path | None) -> tuple[Leg, ...]:
    """Return ``DEFAULT_LEGS`` for ``None``, else the legs of the JSON file at ``path``.

    The file is ``{"legs": [{Leg fields}]}`` and fully replaces the defaults.  Raises
    ``ValueError`` listing every problem found.
    """
    if path is None:
        return DEFAULT_LEGS
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read legs file: {exc.__class__.__name__}") from exc
    except ValueError as exc:
        raise ValueError(f"legs file is not valid JSON: {exc}") from exc
    if not isinstance(doc, Mapping) or not isinstance(doc.get("legs"), list):
        raise ValueError('legs file must be an object {"legs": [...]}')
    problems: list[str] = []
    legs = [_build_leg(i, raw, problems) for i, raw in enumerate(doc["legs"])]
    if not doc["legs"]:
        problems.append("legs: must not be empty")
    ids = [leg.id for leg in legs if leg is not None]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"legs: duplicate id {dup!r}")
    if problems:
        raise ValueError("invalid legs file: " + "; ".join(problems))
    return tuple(legs)  # type: ignore[arg-type]


def with_measured(legs: Iterable[Leg], measured: Mapping[str, int]) -> tuple[Leg, ...]:
    """Return copies of ``legs`` with ``max_request_tokens`` taken from ``measured``.

    ``measured`` maps leg id to the measured window; ids not in ``legs`` are ignored.
    A measured value must be a positive int, otherwise ``ValueError``.
    """
    out = []
    for leg in legs:
        if leg.id in measured:
            value = measured[leg.id]
            if not _is_int(value) or value <= 0:
                raise ValueError(f"measured window for {leg.id!r} must be a positive int")
            leg = dataclasses.replace(leg, max_request_tokens=value)
        out.append(leg)
    return tuple(out)
