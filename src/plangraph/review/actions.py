"""Turn a judged review verdict into planned actions (pure, stdlib only).

No keeper or inbox writes happen here: the caller posts the fix cards and
the inbox payloads. A halt payload is exactly the body ``plangraph.inbox.routes._ask``
reads, ``{key, text, default_answer, deadline, risk_tags}``, with
``default_answer`` and ``deadline`` always ``None`` (never a default answer).
The default call is still pure: an optional ``shadow`` hook on
:func:`plan_actions` only observes a deep copy of the decision (it may record the
answer elsewhere, e.g. a keeper sink), and can never change the plan returned.
"""
from __future__ import annotations

import copy
from typing import Any

__all__ = ["plan_actions"]


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Field from a dict or an attribute-style verdict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _word(verdict: Any) -> str | None:
    """Verdict word (pass/fix/halt) from a str, dict or Verdict."""
    word = verdict if isinstance(verdict, str) else _get(verdict, "verdict")
    return word if word in ("pass", "fix", "halt") else None


def _files(value: Any) -> list[str]:
    """File list from a list/tuple/set or ONE bare string (never iterated per character)."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(f) for f in value]
    return [str(value)]


def _finding(item: Any, phase: str, n: int) -> dict:
    """Normalise one finding (str or dict) to title/finding/files."""
    if not isinstance(item, dict):
        text, files, title = str(item if item is not None else "").strip(), [], ""
    else:
        text = str(item.get("finding") or item.get("text") or "").strip()
        files = _files(item.get("files"))
        title = str(item.get("title") or "").strip()
    if not title:
        first = text.splitlines()[0] if text else "finding"
        title = "Review fix (%s) #%d: %s" % (phase, n, first[:80])
    return {"title": title, "finding": text, "files": files}


def _plan(verdict: Any, phase: str, key: str | None) -> dict:
    """The pure plan for a verdict at a phase (the decision ``plan_actions`` returns)."""
    word = _word(verdict)
    plan: dict = {"fix_cards": [], "inbox": [], "pass": False}
    if word == "pass":
        allowed = _get(verdict, "pass_allowed", True)
        plan["pass"] = bool(allowed)
        return plan
    reason = str(_get(verdict, "reason") or "").strip()
    if word == "fix":
        findings = list(_get(verdict, "findings") or [])
        if not findings:
            findings = [reason or "review asked for a fix without details"]
        files = _files(_get(verdict, "files"))
        for n, item in enumerate(findings, 1):
            card = _finding(item, phase, n)
            if not card["files"]:
                card["files"] = list(files)
            plan["fix_cards"].append(card)
    elif word == "halt":
        if not key:
            raise ValueError("a halt verdict needs the card key (key=...) for the inbox question")
        tags = [str(t) for t in (_get(verdict, "risk_tags") or ["review-halt"])]
        plan["inbox"].append({
            "key": key,
            "text": "Phase %s review halted: %s" % (
                phase, reason or "reviewer halted without a reason"),
            "default_answer": None,
            "deadline": None,
            "risk_tags": tags,
        })
    return plan


def plan_actions(verdict: Any, phase: str, key: str | None = None,
                 *, shadow: Any = None) -> dict:
    """Plan {fix_cards, inbox, pass} for a verdict at a phase.

    fix: one fix card per finding (one generic card when none are listed);
    halt: one inbox question for plan *key* (required: ValueError when missing), no default;
    pass: nothing to do. A verdict
    with no usable word plans nothing and does not pass.

    ``shadow`` is an optional observation-only callable
    ``(verdict, phase, key, plan) -> None``.  It is handed a ``copy.deepcopy`` of the
    verdict (the original verdict when that deepcopy fails) and of the plan, its
    exceptions are swallowed, and the plan returned here is always the plan this
    function computed - a shadow can never change a decision, and an internal
    ``ValueError`` from a halt without a key is raised before any shadow runs.  A
    caller-supplied shadow MUST be time-bounded
    (:func:`plangraph.planner.jev_shadow.qc_hook` is); nothing is written here.
    """
    plan = _plan(verdict, phase, key)
    if shadow is not None:
        try:
            try:
                verdict_copy = copy.deepcopy(verdict)
            except Exception:  # noqa: BLE001 - an uncopyable verdict is observed as-is
                verdict_copy = verdict
            shadow(verdict_copy, phase, key, copy.deepcopy(plan))
        except Exception:  # noqa: BLE001 - an observation must never change the decision
            pass
    return plan
