"""Turn a judged review verdict into planned actions (pure rules, stdlib only).

The rules below are pure: no keeper or inbox writes happen here, the caller posts
the fix cards and the inbox payloads. A halt payload is exactly the body
``plangraph.inbox.routes._ask`` reads, ``{key, text, default_answer, deadline,
risk_tags}``, with ``default_answer`` and ``deadline`` always ``None`` (never a
default answer).

Observe-only shadow (RT-P1-JEV-SHADOW): after the plan is computed, ONE QC row
(``qc_accept``) may be recorded through the Jev shadow hook - the accept decision
this function makes (accept = ``pass``, reject = ``fix``/``halt``) asked in shadow
of the free Jev model. The hook sees a deep copy of the verdict, every failure is
swallowed, and no Jev behaviour can add, remove or change anything in the returned
plan: the decision stays this function's own.  The default hook writes through the
same sink ``route_with_shadow`` uses (keeper store, else JSONL log) and only runs
when Jev's switch ``PLANGRAPH_JEV`` is on.
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


def _shadow_qc(verdict: Any, phase: str, key: str | None, *, shadow, store,
               principal, log_path) -> None:
    """Record Jev's QC answer beside the plan; observe-only, never raises.

    The hook - the caller's ``shadow`` or the default one - receives a deep copy of
    ``verdict``, so it can never mutate the caller's object or the plan, and every
    escape is swallowed.  The default hook goes through
    :func:`plangraph.planner.shadow.qc_with_shadow`: the keeper store when ``store``
    and ``principal`` are given, else the JSONL log path, and it asks anything only
    when Jev's own switch ``PLANGRAPH_JEV`` is on (off is a cheap no-op).
    """
    try:
        observed = copy.deepcopy(verdict)
        if shadow is None:
            # Local import: the planner is only needed when a hook can run.
            from plangraph.planner import shadow as shadow_mod
            shadow_mod.qc_with_shadow(observed, phase, key, log_path=log_path,
                                      store=store, principal=principal)
        else:
            shadow(observed, phase, key)
    except Exception:  # noqa: BLE001 - a shadow failure never breaks the plan
        pass


def plan_actions(verdict: Any, phase: str, key: str | None = None, *,
                 shadow=None, store=None, principal=None,
                 log_path=None) -> dict:
    """Plan {fix_cards, inbox, pass} for a verdict at a phase.

    fix: one fix card per finding (one generic card when none are listed);
    halt: one inbox question for plan *key* (required: ValueError when missing), no default;
    pass: nothing to do. A verdict
    with no usable word plans nothing and does not pass.

    ``shadow`` replaces the observe-only QC hook (called with a deep copy of the
    verdict, ``phase`` and ``key``); without it the Jev shadow hook runs when its
    switch ``PLANGRAPH_JEV`` is on and records one ``qc_accept`` row through
    ``store``/``principal``, else through ``log_path``.  Either way the plan is
    computed first and returned unchanged.
    """
    word = _word(verdict)
    plan: dict = {"fix_cards": [], "inbox": [], "pass": False}
    if word == "pass":
        allowed = _get(verdict, "pass_allowed", True)
        plan["pass"] = bool(allowed)
    elif word == "fix":
        reason = str(_get(verdict, "reason") or "").strip()
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
        reason = str(_get(verdict, "reason") or "").strip()
        tags = [str(t) for t in (_get(verdict, "risk_tags") or ["review-halt"])]
        plan["inbox"].append({
            "key": key,
            "text": "Phase %s review halted: %s" % (
                phase, reason or "reviewer halted without a reason"),
            "default_answer": None,
            "deadline": None,
            "risk_tags": tags,
        })
    _shadow_qc(verdict, phase, key, shadow=shadow, store=store,
               principal=principal, log_path=log_path)
    return plan
