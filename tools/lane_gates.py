#!/usr/bin/env python3
"""Lane gates for CI (PG-EFFICIENCY-GATES, D-2739 §0.2b + §0.2f).

``check --base <main sha> --head <lane sha>``:

* size: changed lines/files vs ``merge-base(base, head)``; warn above 200/8,
  fail above 400/15 unless a ``Plan-Slice: docs/design/<ITEM>.md#<n>`` commit
  trailer names a design slice whose ``max_lines`` covers the lane;
* base pin: at most one merge from main inside the lane (the L1 merges main
  once, right before the final review).

``seats <file> <file> [...]``: first-review round over seat output files
(§0.2c): every seat must return text and at least two seats must run.

Exit 0 pass (warnings printed), 1 a gate failed (seats: changes requested),
2 git or usage error, 3 seats gave no round verdict (re-run the empty seats).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from plangraph.gates.lane import (MAX_MAIN_MERGES, count_numstat, design_slices,  # noqa: E402
                                  parse_slice_trailer, size_verdict)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _is_ancestor(repo, a, b):
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0


def main_merges(repo, base, head, mb):
    """Merge commits in ``mb..head`` that bring in a commit of *base* (main)."""
    out = []
    for row in _git(repo, "rev-list", "--merges", "--parents", f"{mb}..{head}").splitlines():
        sha, *parents = row.split()
        if any(_is_ancestor(repo, p, base) for p in parents[1:]):
            out.append(sha)
    return out


def slice_budget(repo, head, mb):
    """Budget of the lane's declared design slice, or ``None``; errors are messages."""
    values = [v.strip() for v in _git(
        repo, "log", "--format=%(trailers:key=Plan-Slice,valueonly)", f"{mb}..{head}").splitlines()
        if v.strip()]
    if not values:
        return None, None
    declared = {parse_slice_trailer(v) for v in values}
    if None in declared or len(declared) != 1:
        return None, f"Plan-Slice trailers must name one docs/design/<ITEM>.md#<n>: {sorted(set(values))}"
    path, n = declared.pop()
    try:
        text = _git(repo, "show", f"{head}:{path}")
    except subprocess.CalledProcessError:
        return None, f"Plan-Slice names {path}, which is not in the lane head"
    budget = design_slices(text).get(n)
    if budget is None:
        return None, f"{path} has no slice {n} in its '## Slices' table"
    return budget, None


def check(repo, base, head):
    mb = _git(repo, "merge-base", base, head).strip()
    failed = False
    lines, files, _ = count_numstat(_git(repo, "diff", "--numstat", "-M", mb, head))
    budget, problem = slice_budget(repo, head, mb)
    if problem:
        print(f"lane-gates: size FAIL: {problem}")
        failed = True
    else:
        level, message = size_verdict(lines, files, budget)
        print(f"lane-gates: size {level.upper()}: {message}")
        failed = failed or level == "fail"
    merges = main_merges(repo, base, head, mb)
    if len(merges) > MAX_MAIN_MERGES:
        print(f"lane-gates: base FAIL: {len(merges)} merges from main ({', '.join(m[:10] for m in merges)});"
              f" max {MAX_MAIN_MERGES}, right before the final review (D-2739 §0.2f)")
        failed = True
    else:
        print(f"lane-gates: base OK: {len(merges)} merge(s) from main")
    return 1 if failed else 0


def seats(paths):
    from plangraph.gates.findings import first_review
    outputs = {}
    for path in paths:
        try:
            outputs[Path(path).stem] = Path(path).read_text("utf-8", "replace")
        except OSError as exc:
            print(f"lane-gates: seats: cannot read {path}: {type(exc).__name__}", file=sys.stderr)
            return 2
    outcome, reasons = first_review(outputs)
    for reason in reasons:
        print(f"lane-gates: seat {reason}")
    print(f"lane-gates: first review {(outcome or 'no verdict, re-run').upper()}")
    return {"pass": 0, "changes": 1}.get(outcome, 3)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--base", required=True)
    c.add_argument("--head", required=True)
    c.add_argument("--repo", default=".")
    s = sub.add_parser("seats")
    s.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "seats":
        return seats(args.files)
    try:
        return check(args.repo, args.base, args.head)
    except subprocess.CalledProcessError as exc:
        print(f"lane-gates: git failed: {' '.join(exc.cmd[3:])}: {exc.stderr.strip()[:200]}",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
