"""Pure advisory stop rule over frozen state and parsed seat results.

The driver supplies report={sha, ok, claims: [id]} and claims={id: row}
in the manifest, alongside plan_chars. No material is read by this module.
Persist capped/stalled and waiver decisions in the manifest; only the driver
may clear a cap when extending it. Input rows are never modified.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from plangraph.planner.seats import seat_family
from tools import plan_loop_ledger


@dataclass(frozen=True)
class Status:
    state: str
    line: str
    rounds: int
    families: tuple[str, ...] = ()
    next: str = ""
    open_questions: tuple[str, ...] = ()

    def __str__(self):
        return self.line

    @property
    def exit_code(self):
        return 0 if self.state == "CLEAN" else 1


def _completed(row):
    return row.get("status") == "completed"


def _witness(row, policy):
    if row.get("witness_kind") == "operator-chat":
        return bool(re.fullmatch(r"D-\d+", row.get("decision", ""))
                    and row.get("decision_by")
                    and row["decision_by"] != policy.get("planner", "planner"))
    # The spawn adapter validates served identity and request-side model
    # equality before setting witnessed; never infer it from a verdict.
    return row.get("witnessed") is True


def _quiet(row):
    counts = row.get("counts", {})
    return not (counts.get("blocker", 0) + counts.get("major", 0))


def round_count(seats):
    """Count completed round verbs, not seats or failed launches."""
    completed = [s for s in seats if _completed(s)]
    confirms = list(dict.fromkeys(s["round"] for s in completed if s.get("kind") == "confirm"))
    counted = {s["round"] for s in completed
               if s.get("kind") in {"discovery", "stacked", "scope-cut"}}
    counted.update(confirms[1:])
    return len(counted)


def _claims(manifest, policy, material_sha, history):
    report = manifest.get("report", {})
    ids = report.get("claims", [])
    if not report.get("ok") or report.get("sha") != material_sha or not ids:
        return False, ()
    questions = []
    settled = True
    for key in ids:
        claim = manifest.get("claims", {}).get(key, {})
        state = claim.get("state")
        if state == "false":
            fixed = claim.get("sha_fixed")
            settled &= fixed in history and history.index(fixed) > history.index(report["sha"])
        elif state == "open-question":
            questions.append(key)
            settled &= policy.get("tier", "standard") != "high"
        elif state in {"verified", "not-load-bearing"}:
            # Originally verified claims need no disposition evidence.
            settled &= (state == "verified" and claim.get("original_state", "verified") == "verified"
                        or bool(claim.get("evidence")))
        else:
            settled = False
    return bool(settled), tuple(questions)


def evaluate(ledger, manifest, seats, policy):
    """Return a printable Status; no write, implicit waiver, or witness override.

    `capped` is a sticky driver latch. `stalled` also represents exhaustion or
    insufficient high-tier discovery availability before launching any seat.
    Decisions in operator-chat rows must already be recorded independently.
    """
    slug, sha = policy.get("slug", manifest.get("slug", "plan")), manifest["text_sha"]
    rounds = round_count(seats)
    history_rows = manifest.get("history", [])
    history = [r["sha"] for r in history_rows]
    current = history.index(sha)
    material_index = max((i for i in range(current + 1) if history_rows[i].get("material", True)), default=0)
    material_sha = history[material_index]
    completed = [s for s in seats if _completed(s)]
    valid_shas = {sha}
    pending_diff = False
    for i in range(current, material_index, -1):
        step = history_rows[i]
        diffs = [s for s in completed if s.get("kind") == "diff"
                 and s.get("sha") == step["sha"] and s.get("from_sha") == step.get("from_sha")]
        if any(s.get("verdict") != "ACCEPT" or not _quiet(s) for s in diffs):
            break
        if not any(_witness(s, policy) and s.get("verdict") == "ACCEPT" for s in diffs):
            pending_diff = True
            break
        valid_shas.add(history[i - 1])
    accepts = [s for s in completed if s.get("kind") not in {"diff", "r0"}
               and s.get("sha") in valid_shas and s.get("verdict") == "ACCEPT" and _quiet(s)]
    families = tuple(sorted({seat_family(s["family"]) for s in accepts if _witness(s, policy)}))
    unwitnessed = sorted({seat_family(s["family"]) for s in accepts if not _witness(s, policy)})
    operator = sorted({seat_family(s["family"]) for s in completed if s.get("witness_kind") == "operator-chat"})
    # Reuse A1's closure semantics on copies to preserve the pure interface.
    rows = [dict(r) for r in ledger]
    closable = [r for r in rows if r.get("disposition") == "accepted" and r.get("sha_fixed") in history]
    plan_loop_ledger.close_accepted(closable, manifest, [dict(s, witnessed=True) for s in completed if _witness(s, policy)])
    open_rows = [r for r in rows if not r.get("dup_of") and
                 (r.get("disposition", "open") == "open" or
                  r.get("disposition") == "accepted" and not r.get("closed_by"))]
    blockers = sum(r["severity"] == "blocker" for r in open_rows)
    majors = sum(r["severity"] == "major" for r in open_rows)
    unverified = sum(bool(r.get("unverified")) for r in open_rows)
    latest = [s for s in completed if s.get("sha") == sha and _witness(s, policy) and s.get("kind") != "r0"]
    quiet = bool(latest and _quiet(latest[-1]) and not latest[-1].get("new_classes"))
    if latest and latest[-1].get("kind") == "diff":
        quiet &= latest[-1].get("verdict") == "ACCEPT"
    stacked = [s for s in completed if s.get("kind") == "stacked"]
    scope = any(s.get("kind") == "scope-cut" for s in completed) or (
        policy.get("tier", "standard") == "standard" and bool(stacked) and stacked[-1].get("lens") == "scope-cut")
    claims_ok, questions = _claims(manifest, policy, material_sha, history)
    baseline = manifest.get("plan_chars_r1")
    chars = manifest.get("plan_chars")
    cite = history_rows[current].get("growth_cite")
    growth = any(r.get("id") == cite and r.get("claim") for r in ledger) if cite else False
    size = growth or (baseline is not None and chars is not None and chars <= policy.get("size_growth", 1.15) * baseline)
    clean = not (blockers or majors or pending_diff) and quiet and scope and claims_ok and size and len(families) >= policy.get("accept_families", 4)
    capped = manifest.get("capped", False)
    cap_now = rounds >= policy.get("max_rounds", 6) and not clean
    reason = "review-loop-capped" if capped or cap_now else ""
    options = "raise cap by D-row, *replan"
    if not reason:
        failures = {}
        for s in seats:
            if s.get("attempt_used", True) and s.get("status") in {"failed", "no_verdict", "timed_out"}:
                failures[s.get("round")] = failures.get(s.get("round"), 0) + 1
        stalled = manifest.get("stalled") or (seats and failures.get(seats[-1].get("round"), 0) >= policy.get("max_failed_attempts", 3))
        if stalled:
            reason, options = "review-loop-stalled", "wait for a family, *waive by D-row"
        if manifest.get("ladder_exhausted"):
            reason, options = "ladder-exhausted", "add a leg, *replan"
    waived = manifest.get("waived")
    if waived and reason != "review-loop-capped" and not set(policy.get("tags", [])) & {"auth", "access", "secrets"}:
        return Status("WAIVED", f"WAIVED plan:{slug} {waived}", rounds, families)
    if reason:
        return Status("BLOCKED", f"BLOCKED plan:{slug} | {reason} | options: {options}", rounds, families)
    if clean:
        return Status("CLEAN", f"CLEAN plan:{slug} manifest={sha} families={','.join(families)}", rounds, families, open_questions=questions)
    next_kind = "diff" if pending_diff else "r0" if not claims_ok else "discovery" if not rounds else "scope-cut" if not scope else "confirm"
    line = (f"OPEN plan:{slug} round={rounds} open={blockers}/{majors} unverified={unverified} "
            f"accepted={len(families)}/{policy.get('accept_families', 4)} "
            f"unwitnessed_accepts={','.join(unwitnessed)} operator_chat={','.join(operator)} next={next_kind}")
    return Status("OPEN", line, rounds, families, next_kind, questions)
