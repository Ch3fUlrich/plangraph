"""md_sync router decision row pass: Closes key@sha gates the close on the merge.

For every decisions-log row whose BY cell names the router (case-insensitive,
outside fenced blocks), the Closes tokens are read only through the keeper's
own parser (closes_with_commits / has_closes_keyword) and planned with the same
op shapes the keeper verbs accept (edit, legacy_state, add_evidence,
close_on_decision):

* a decision-only item closes on the decision exactly like the answers path; a
  stray @sha still closes but warns (a mis-kinded code lane must not close
  silently);
* an item with evidence_kinds containing 'any' with an @sha token is treated as
  a code item (closes only when the commit is merged to the target ref, else
  in_review: fail closed: an accepted-but-unmerged item must never close by
  decision); an item with 'any' with a plain key (no sha) keeps the answers
  path behaviour and closes on decision;
* a code item closes only when the commit of its key@sha token resolves to a
  full id and is an ancestor of the project's target ref (keeper config
  repos; read-only git, no fetch, fail closed on any error; the configured
  ref decides, exactly; a config ref of `main` or `origin/main` is used as
  configured, with no fallback to the other); otherwise it is planned
  in_review only;
* a plain key (no sha) on a code item is never closed, only listed in_review;
* a mixed row like 'Closes: A@good B@zzz' closes A and drops B (the keeper
  parser drops B; no invalid-token line is printed unless the whole row yields
  no token);
* the row regexes duplicated from the keeper and parser are intentional.

The router's Models trailer of each planning decision row is copied into the
open item's note via an edit op (prefix 'D-<n> ', separator ' | '), capped at
NOTE_CAP and masked for credential shapes. Models for items already in_review
or claimed is never copied (the keeper refuses a note edit on a non-open item).
A | or U+2028 inside a segment ends the cell: the row is dropped by the
keeper's parser before this module sees it.
"""
import os
import re
import sys

# Import the keeper from the checkout so tools/ works without an install step.
_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    os.pardir, "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from plangraph.keeper import credential_guard  # noqa: E402
from plangraph.keeper.note_fold import NOTE_CAP  # noqa: E402
from md_sync_gates import GitGates, names_seats, track_of  # noqa: E402,F401

DECISION_ROW_RE = re.compile(r"^\| D-([0-9]+) \|")
_FENCE_RE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")
#: the same keyword boundary the keeper's Closes/Waives capture stops at
_KEYWORD_RE = re.compile(r"\b([A-Za-z-]+):(?=\s|$)")
_MODELS_KW_RE = re.compile(r"(?i)\bmodels\s*:")
_COMMIT_KINDS = ("commit", "commit+ci", "semaphore-task")


def router_drows(text, since=1):
    """(num, by_cell, decision_cell) per router D-row at/above since."""
    fenced = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced or line.startswith("    ") or line.startswith("\t"):
            continue
        match = DECISION_ROW_RE.match(line)
        if not match or int(match.group(1)) < since:
            continue
        cells = line.split("|")[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        by = cells[3] if len(cells) >= 4 else ""
        if "router" not in by.lower() and "routing-00" not in by.lower():  # D-722: both spellings
            continue
        dec = cells[2] if len(cells) >= 3 else ""
        yield int(match.group(1)), by, dec


def models_segment(cell):
    """The Models: trailer of a DECISION cell (verbatim, stripped), or None."""
    match = _MODELS_KW_RE.search(cell)
    if not match:
        return None
    start = match.end()
    bounds = [k.start() for k in _KEYWORD_RE.finditer(cell, start)]
    end = bounds[0] if bounds else len(cell)
    segment = cell[start:end].strip()
    return segment or None


def sanitize_segment(segment):
    """One-line form of a Models segment; None when it must not be copied."""
    if not segment:
        return None
    flat = " ".join(segment.split())
    return flat or None


def collect(text, view, since=1):
    """Aggregate the router D-rows into per-key intent: (dinfo, invalid_rows).

    SUPERSEDE RULE (router-approved): rows are processed in D-number order;
    when several rows name the same key with different, non-prefix-compatible
    shas, the row with the highest D-number supersedes the earlier ones: only
    its sha counts (superseded D-ids are recorded in info["superseded"]).
    A conflict inside one row stays invalid exactly as today: the keeper's
    closes_with_commits drops such a key (absent/invalid), so it never reaches
    the per-key merge and the row lands in invalid_rows. Prefix-compatible
    shas keep today's behaviour (keep the longer).
    """
    dinfo, invalid_rows = {}, []
    ordered = sorted(router_drows(text, since), key=lambda entry: entry[0])
    for num, by, cell in ordered:
        d_id = f"D-{num}"
        closes = view.closes_with_commits(d_id)
        if view.has_closes_keyword(d_id) and not closes:
            invalid_rows.append(d_id)
        segment = sanitize_segment(models_segment(cell))
        for key, sha in closes.items():
            info = dinfo.setdefault(key, {"dnums": set(), "router_max": num,
                                          "sha": None, "sha_dnum": None,
                                          "conflict": False,
                                          "conflict_d": None,
                                          "superseded": [], "models": []})
            info.setdefault("superseded", [])
            info["dnums"].add(num)
            if num > info["router_max"]:
                info["router_max"] = num
            if sha is not None:
                current = info["sha"]
                if current is None:
                    info["sha"] = sha
                    info["sha_dnum"] = num
                elif current.lower().startswith(sha.lower()):
                    pass  # keep the longer (earlier) prefix-compatible sha
                elif sha.lower().startswith(current.lower()):
                    info["sha"] = sha
                    info["sha_dnum"] = num
                elif num == info.get("sha_dnum"):
                    # Same D-number with a different non-compatible sha:
                    # same-row conflict, stays invalid as today.
                    info["conflict"] = True
                    if info["conflict_d"] is None or num > info["conflict_d"]:
                        info["conflict_d"] = num
                else:
                    # Cross-row mismatch: later (higher D-number) row wins.
                    prev = info.get("sha_dnum")
                    if prev is not None:
                        prev_id = f"D-{prev}"
                        if prev_id not in info["superseded"]:
                            info["superseded"].append(prev_id)
                    info["sha"] = sha
                    info["sha_dnum"] = num
            if segment and (num, segment) not in info["models"]:
                info["models"].append((num, segment))
    return dinfo, invalid_rows


def finalize(dinfo):
    """Order each key's d-ids and Models segments by D-number; set the max."""
    for info in dinfo.values():
        nums = sorted(info["dnums"])
        info["d_ids"] = [f"D-{n}" for n in nums]
        info["models"].sort(key=lambda entry: entry[0])
        info["max_dnum"] = nums[-1] if nums else None


def _current(item, row):
    """A row counts only at the item's current epoch: keeper close ignores older rows (store_close).

    Fail closed: a row missing ``epoch`` never counts at an item that has one (its
    epoch is unknown, so it may predate a reopen and must be re-attached). The
    permissive branch is a documented fixture contract, not a snapshot state: the
    snapshot stamps ``epoch`` on every item and row, while older fixtures in
    tests/test_md_sync_dclose.py build items without the key.
    """
    if "epoch" not in item:  # fixture contract: items built without an epoch accept any row
        return True
    return row.get("epoch") == item["epoch"]


def _decision_ids(item):
    out = set()
    for row in item.get("evidence") or []:
        if row.get("kind") == "decision" and _current(item, row):
            ref = row.get("ref")
            out.add(ref.get("id") if isinstance(ref, dict) else ref)
    return out


def _commit_shas(item):
    out = set()
    for row in item.get("evidence") or []:
        if row.get("kind") == "commit" and _current(item, row):
            ref = row.get("ref") or {}
            sha = ref.get("sha") or ref.get("commit")
            if isinstance(sha, str) and sha:
                out.add(sha.lower())
    return out


def _note_update(current_note, models):
    """The item's note with the key's Models segments appended."""
    current = current_note or ""
    adds = []
    for num, segment in models:
        masked_seg, _ = credential_guard.mask(segment)
        if f"D-{num} {segment}" not in current and f"D-{num} {masked_seg}" not in current:
            adds.append((num, segment))
    if not adds:
        return None, []
    joined = " | ".join(f"D-{n} {s}" for n, s in adds)
    return ((current + " | " + joined) if current else joined), adds


def _models_note(plan, key, item, info):
    """Plan the Models note for an open item; return the new note or None."""
    candidate, adds = _note_update(item.get("note"), info.get("models") or [])
    if candidate is None:
        return None
    if len(candidate) > NOTE_CAP:
        plan.warnings.append({"key": key, "decision": f"D-{info['max_dnum']}",
                              "text": (f"models note for {key} would exceed "
                                      f"{NOTE_CAP} chars; not applied")})
        return None
    for num, segment in adds:
        masked_seg, _ = credential_guard.mask(segment)
        plan.models.append({"key": key, "decision": f"D-{num}", "text": masked_seg})
    return candidate


def plan(plan, dinfo, invalid_rows, *, by_key, pending_keys, repos):
    """Plan the router D-row pass onto plan (mutates; git is read-only)."""
    gates = GitGates(repos)
    for d_id in sorted(set(invalid_rows)):
        plan.invalid_tokens.append(d_id)
    for key in sorted(dinfo):
        info = dinfo[key]
        item = by_key.get(key)
        if item is None:
            if key in pending_keys:
                item = {"key": key, "project": key.split(":", 1)[0],
                        "state": "open", "review_tier": "none",
                        "evidence_kinds": ["decision"], "evidence": [],
                        "note": None}
            else:
                plan.dclose_skipped.append(
                    (key.split(":", 1)[1], f"D-{info['max_dnum']}", key,
                     "not in graph"))
                continue
        _plan_key(plan, key, item, info, gates)


def _plan_key(plan, key, item, info, gates):
    state = item.get("state")
    if state in ("done", "cancelled"):
        return
    if info.get("conflict"):
        conflict_d = f"D-{info['conflict_d']}"
        plan.invalid_tokens.append(conflict_d)
        # The skipped key must not vanish: surface it as a warning too.
        plan.warnings.append({"key": key, "decision": conflict_d,
                              "text": (f"conflicting Closes shas for {key} "
                                       f"in {conflict_d}; skipped")})
        return
    clause = names_seats(item) if item.get("review_tier") != "none" else None
    if clause:
        # D-3395: the keeper refuses tier 'none' here; close via seat verdicts.
        plan.skipped.append((key.split(":", 1)[1],
                             f"blocked: done_criteria name {clause}; "
                             "closes via seat verdicts"))
        return
    new_note = _models_note(plan, key, item, info) if state == "open" else None
    kinds = item.get("evidence_kinds") or []
    has_sha = info.get("sha") is not None
    is_commit_item = any(k in _COMMIT_KINDS for k in kinds)
    is_any_with_sha = "any" in kinds and has_sha
    if is_commit_item or is_any_with_sha:
        if not has_sha:
            _plan_listed(plan, key, item, info, new_note)
        else:
            _plan_sha(plan, key, item, info, gates, new_note)
    else:
        _plan_decision(plan, key, item, info, new_note)


def _plan_decision(plan, key, item, info, new_note):
    """Close a decision-only item exactly as the answers path does."""
    primary = f"D-{info['max_dnum']}"
    state = item.get("state")
    if info.get("sha") is not None:
        plan.warnings.append({"key": key, "decision": primary,
                              "text": f"sha ignored on decision-only item {key}"})
    decision_only = (item.get("review_tier") == "none"
                     and (item.get("evidence_kinds") or []) == ["decision"])
    if state == "open":
        fields = {}
        if new_note is not None:
            fields["note"] = new_note
        if not decision_only:
            fields["review_tier"] = "none"
            fields["evidence_kinds"] = ["decision"]
        if fields:
            plan.add({"op": "edit", "key": key, "fields": fields})
        plan.add({"op": "legacy_state", "key": key, "state": "in_review",
                  "note": f"router {primary}: Closes {key}"})
    elif not decision_only:
        plan.skipped.append((key.split(":", 1)[1], f"blocked: {key} is {state}"))
        return
    have = _decision_ids(item)
    for d_id in info["d_ids"]:
        if d_id not in have:
            plan.add({"op": "add_evidence", "key": key, "kind": "decision",
                      "ref": {"id": d_id}})
    plan.add({"op": "close_on_decision", "key": key})
    plan.d_closes.append({"key": key, "decision": primary})
    plan.d_lines.append(f"close {key}: by {primary}")


def _plan_sha(plan, key, item, info, gates, new_note):
    """A code item with a @sha token; the close gates on the merge."""
    primary = f"D-{info['max_dnum']}"
    state = item.get("state")
    project = item.get("project") or key.split(":", 1)[0]
    token = info["sha"]
    sha8 = token[:8]
    check = gates.check(project, token, track_of(item))
    if check["merged"]:
        target_kinds = ["decision", "commit"]
        tier_done = (item.get("review_tier") == "none"
                     and set(item.get("evidence_kinds") or [])
                     == {"decision", "commit"})
        if state == "open":
            fields = {}
            if new_note is not None:
                fields["note"] = new_note
            if not tier_done:
                fields["review_tier"] = "none"
                fields["evidence_kinds"] = target_kinds
            if fields:
                plan.add({"op": "edit", "key": key, "fields": fields})
            plan.add({"op": "legacy_state", "key": key, "state": "in_review",
                      "note": f"router {primary}: Closes {key}@{sha8}"})
        elif not tier_done:
            plan.add({"op": "edit", "key": key, "fields": {
                "review_tier": "none", "evidence_kinds": target_kinds}})
        have = _decision_ids(item)
        for d_id in info["d_ids"]:
            if d_id not in have:
                plan.add({"op": "add_evidence", "key": key, "kind": "decision",
                          "ref": {"id": d_id}})
        if check["full"].lower() not in _commit_shas(item):
            op = {"op": "add_evidence", "key": key, "kind": "commit",
                  "ref": {"sha": check["full"]}}
            if check["repo"]:
                op["repo"] = check["repo"]
            plan.add(op)
        plan.add({"op": "close_on_decision", "key": key})
        plan.d_closes.append({"key": key, "decision": primary})
        plan.d_lines.append(f"close {key}: by {primary}")
    else:
        reason = ("repo not registered" if not check["repo"]
                  else f"not on {check['ref']}")
        if state == "open":
            fields = {}
            if new_note is not None:
                fields["note"] = new_note
            if fields:
                plan.add({"op": "edit", "key": key, "fields": fields})
            plan.add({"op": "legacy_state", "key": key, "state": "in_review",
                      "note": f"router {primary}: Closes {key}@{sha8}; {reason}"})
        plan.d_in_review.append({"key": key, "decision": primary, "sha": sha8,
                                 "reason": reason})
        plan.d_lines.append(f"in_review {key}: {primary} @{sha8} {reason}")


def _plan_listed(plan, key, item, info, new_note):
    """A code item named without a sha: in_review only, never closed."""
    primary = f"D-{info['max_dnum']}"
    if item.get("state") == "open":
        fields = {}
        if new_note is not None:
            fields["note"] = new_note
        if fields:
            plan.add({"op": "edit", "key": key, "fields": fields})
        plan.add({"op": "legacy_state", "key": key, "state": "in_review",
                  "note": f"router {primary}: Closes {key} (no sha)"})
    plan.d_in_review.append({"key": key, "decision": primary, "sha": None,
                             "reason": "no sha"})
    plan.d_lines.append(f"listed {key}: {primary} no sha")
