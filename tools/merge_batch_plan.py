"""Planning for tools/merge_batch.py: stack order, HIGH tier, batch caps, the plan table (pure)."""
from __future__ import annotations

import fnmatch

from merge_batch_util import NEEDS_UPDATE, MergeBatchError

try:
    from ci_commit_gate import AUTH_DOOR_PATHS, GATE_PATHS
except ImportError:  # merge_batch copied away from its sibling: the caller names the paths
    AUTH_DOOR_PATHS, GATE_PATHS = (), ()

#: Always HIGH, even if the gate's own list is renamed.
KEEPER_PATHS = ("src/plangraph/keeper/*",)


def high_patterns(extra=()):
    """The AUTH_DOOR + gate globs of tools/ci_commit_gate.py, the keeper tree and --high-path."""
    if not AUTH_DOOR_PATHS and not extra:
        raise MergeBatchError("tools/ci_commit_gate.py is not importable, so the HIGH path list "
                              "is unknown: pass --high-path GLOB (repeatable)")
    return tuple(dict.fromkeys((*AUTH_DOOR_PATHS, *GATE_PATHS, *KEEPER_PATHS, *extra)))


def high_hits(files, patterns):
    """The changed paths that match a HIGH glob."""
    return [path for path in files if any(fnmatch.fnmatchcase(path, pat) for pat in patterns)]


def stack_parents(prs):
    """number -> number of the same-repo PR whose head branch is this PR's base branch."""
    by_ref = {}
    for pr in sorted(prs, key=lambda p: p["number"]):
        if not pr["cross"]:
            by_ref.setdefault(pr["ref"], pr["number"])
    return {pr["number"]: by_ref[pr["base"]] for pr in prs
            if pr["base"] in by_ref and by_ref[pr["base"]] != pr["number"]}


def ancestors(number, parent):
    """(stack parents, nearest first; True when the chain loops back on itself)."""
    seen, chain = {number}, []
    while number in parent:
        number = parent[number]
        if number in seen:
            return chain, True
        seen.add(number)
        chain.append(number)
    return chain, False


def stack_sorted(numbers, parent):
    """Keep the given order, but every PR after the PR it is stacked on."""
    wanted, out = set(numbers), []

    def emit(num, trail=()):
        if num in out:
            return
        if num in trail:
            raise MergeBatchError(f"stack cycle through PR #{num}")
        if parent.get(num) in wanted:
            emit(parent[num], (*trail, num))
        out.append(num)

    for num in numbers:
        emit(num)
    return out


def exclusions(prs, base, parent):
    """number -> why the PR cannot join a batch (own state first, then its stack)."""
    by_num = {pr["number"]: pr for pr in prs}
    why = {}
    for pr in prs:
        if pr["state"] != "OPEN":
            why[pr["number"]] = f"not open ({pr['state'].lower()})"
        elif pr["draft"]:
            why[pr["number"]] = "draft"
        elif pr["ci"] == "red":
            why[pr["number"]] = "ci red"
        elif NEEDS_UPDATE in pr["labels"]:
            why[pr["number"]] = f"label {NEEDS_UPDATE}"
    for pr in prs:
        num = pr["number"]
        chain, cycle = ancestors(num, parent)
        top = by_num[chain[-1]] if chain else pr
        if num in why:
            continue
        if cycle:
            why[num] = "stack cycle"
        elif top["base"] != base:
            why[num] = f"targets {top['base']!r}, not {base!r}"
        elif any(anc in why for anc in chain):
            bad = next(anc for anc in chain if anc in why)
            why[num] = f"stacked on #{bad} ({why[bad]})"
    return why


def plan_batches(prs, base, max_prs=6, max_lines=1200, patterns=()):
    """Order PRs (stacks together, base first), tier them and cut batches by the caps."""
    by_num = {pr["number"]: pr for pr in prs}
    parent = stack_parents(prs)
    why = exclusions(prs, base, parent)
    units = {}
    for pr in prs:
        if pr["number"] not in why:
            chain, _ = ancestors(pr["number"], parent)
            units.setdefault(chain[-1] if chain else pr["number"], []).append(pr["number"])
    unit_list = []
    for members in units.values():
        members = stack_sorted(sorted(members), parent)
        unit_list.append({"prs": members, "lines": sum(by_num[n]["lines"] for n in members),
                          "high": any(high_hits(by_num[n]["files"], patterns) for n in members)})
    unit_list.sort(key=lambda unit: min(unit["prs"]))
    batches, cur = [], None
    for unit in unit_list:
        if unit["high"]:  # a HIGH stack never shares a batch
            batches.append({"tier": "HIGH", "prs": list(unit["prs"]), "lines": unit["lines"]})
            continue
        if cur and (len(cur["prs"]) + len(unit["prs"]) > max_prs or cur["lines"] + unit["lines"] > max_lines):
            cur = None
        if cur is None:
            cur = {"tier": "STANDARD", "prs": [], "lines": 0}
            batches.append(cur)
        cur["prs"] += unit["prs"]
        cur["lines"] += unit["lines"]
    batches.sort(key=lambda batch: min(batch["prs"]))
    for idx, batch in enumerate(batches, 1):
        batch.update(id=idx, base=base, files=len({f for n in batch["prs"] for f in by_num[n]["files"]}),
                     over_cap=len(batch["prs"]) > max_prs or batch["lines"] > max_lines)
    placed = {n: (batch["id"], batch["tier"]) for batch in batches for n in batch["prs"]}
    order = [n for batch in batches for n in batch["prs"]] + sorted(why)
    rows = [{"number": n, "title": by_num[n]["title"], "head": by_num[n]["head"], "ref": by_num[n]["ref"],
             "base": by_num[n]["base"], "draft": by_num[n]["draft"], "ci": by_num[n]["ci"],
             "files": len(by_num[n]["files"]), "lines": by_num[n]["lines"], "paths": by_num[n]["files"],
             "high": bool(high_hits(by_num[n]["files"], patterns)),
             "high_paths": high_hits(by_num[n]["files"], patterns)[:5], "batch": placed.get(n, (None, None))[0],
             "tier": placed.get(n, (None, None))[1], "after": parent.get(n), "reason": why.get(n)}
            for n in order]
    return {"base": base, "max_prs": max_prs, "max_lines": max_lines, "batches": batches, "prs": rows}


def render_table(plan):
    head = ("B", "TIER", "PR", "HEAD", "CI", "DRAFT", "FILES", "LINES", "BASE", "TITLE / NOTE")
    table = [head]
    for row in plan["prs"]:
        note = row["title"][:50]
        if row["reason"]:
            note = f"EXCLUDED: {row['reason']} | {note}"
        elif row["after"]:
            note = f"after #{row['after']} | {note}"
        if row["high_paths"]:
            note = f"HIGH via {row['high_paths'][0]} | {note}"
        table.append((str(row["batch"] or "-"), row["tier"] or "-", f"#{row['number']}", row["head"][:8],
                      row["ci"], "yes" if row["draft"] else "no", str(row["files"]), str(row["lines"]),
                      row["base"], note))
    widths = [max(len(line[i]) for line in table) for i in range(len(head))]
    out = ["  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in table]
    for batch in plan["batches"]:
        flag = "  OVER CAP" if batch["over_cap"] else ""
        out.append(f"batch {batch['id']} {batch['tier']}: --prs {','.join(map(str, batch['prs']))}  "
                   f"({len(batch['prs'])} PRs, {batch['files']} files, {batch['lines']} lines){flag}")
    if not plan["batches"]:
        out.append("no PR is eligible for a batch")
    return "\n".join(out)
