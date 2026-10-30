"""CI affected-suite selector and gate verdict."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

SUITES: tuple[str, ...] = (
    "scrub",
    "pytest",
    "pytest-postgres",
    "rust",
    "rust-worker",
    "rust-spike",
)

KNOWN_PREFIXES: tuple[str, ...] = (
    "src/",
    "tests/",
    "tools/",
    "vendor/",
    "rust/",
    "skills/",
)


def _normalize_path(p: str) -> str:
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _is_ci_deps(p: str) -> bool:
    return (
        p.startswith(".github/")
        or p == "requirements-ci.txt"
        or p == "tools/ci_select.py"
    )


def _is_docs_class(p: str) -> bool:
    basename = p.split("/")[-1]
    return (
        p.endswith(".md")
        or p.startswith("docs/")
        or basename.startswith("CHANGELOG")
    )


def _is_known(p: str) -> bool:
    if _is_docs_class(p):
        return True
    return any(p.startswith(prefix) for prefix in KNOWN_PREFIXES)


def select(paths: list[str]) -> dict[str, bool]:
    norm_paths: list[str] = []
    for raw in paths:
        p = _normalize_path(raw)
        if not p.strip():
            continue
        norm_paths.append(p)

    # 1. No paths at all (empty diff) -> full run (fail closed)
    if not norm_paths:
        res = {s: True for s in SUITES}
        res["full"] = True
        return res

    # 2. Any path that is a CI/deps path -> full run
    if any(_is_ci_deps(p) for p in norm_paths):
        res = {s: True for s in SUITES}
        res["full"] = True
        return res

    # 3. Any UNKNOWN path -> full run
    if any(not _is_known(p) for p in norm_paths):
        res = {s: True for s in SUITES}
        res["full"] = True
        return res

    # 5. Otherwise (no full trigger) start with scrub=True ALWAYS
    res = {s: False for s in SUITES}
    res["scrub"] = True
    res["full"] = False

    for p in norm_paths:
        if p.startswith("skills/"):
            # skill pages are guarded by tests/test_skill_*.py
            res["pytest"] = True
            continue
        if _is_docs_class(p):
            continue
        if any(p.startswith(prefix) for prefix in ("src/", "tests/", "tools/", "vendor/")):
            res["pytest"] = True
            res["pytest-postgres"] = True
        elif p.startswith("rust/"):
            res["pytest"] = True
            res["pytest-postgres"] = True
            if p.startswith("rust/spikes/"):
                res["rust-spike"] = True
            if p.startswith("rust/worker/") or p.startswith("rust/worker-core/"):
                res["rust-worker"] = True
            if (
                (not p.startswith("rust/spikes/") and not p.startswith("rust/worker/"))
                or p.startswith("rust/worker-core/")
            ):
                res["rust"] = True

    return res


def gate(needs: dict) -> list[str]:
    if not isinstance(needs, dict) or "select" not in needs or not isinstance(needs["select"], dict):
        return ["select: missing"]

    select_job = needs["select"]
    select_result = select_job.get("result")
    if select_result != "success":
        return [f"select: {select_result if select_result is not None else 'missing'}"]

    outputs = select_job.get("outputs")
    if not isinstance(outputs, dict):
        outputs = {}

    problems: list[str] = []
    for suite in SUITES:
        val = outputs.get(suite)
        selected = val != "false"
        if not selected:
            continue

        if suite not in needs or not isinstance(needs[suite], dict):
            problems.append(f"{suite}: missing")
        else:
            suite_result = needs[suite].get("result")
            if suite_result != "success":
                problems.append(
                    f"{suite}: {suite_result if suite_result is not None else 'missing'}"
                )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CI selector and gate verdict")
    subparsers = parser.add_subparsers(dest="command")

    select_parser = subparsers.add_parser("select")
    select_parser.add_argument("--full", action="store_true", help="Force full CI run")
    select_parser.add_argument("--base", type=str, default=None, help="Base commit SHA")
    select_parser.add_argument("--head", type=str, default=None, help="Head commit SHA")
    select_parser.add_argument("--files", nargs="*", default=None, help="Explicit list of files")

    gate_parser = subparsers.add_parser("gate")
    gate_parser.add_argument("--needs", type=str, required=True, help="JSON string of needs object")

    args = parser.parse_args(argv)

    if args.command == "select":
        if args.full:
            selection = {s: True for s in SUITES} | {"full": True}
        elif args.base is not None or args.head is not None:
            if args.base is not None and args.head is not None:
                try:
                    proc = subprocess.run(
                        ["git", "diff", "--name-only", "-z", f"{args.base}...{args.head}"],
                        capture_output=True,
                        check=True,
                    )
                    raw_text = proc.stdout.decode("utf-8", errors="replace")
                    paths = [p for p in raw_text.split("\0") if p]
                    selection = select(paths)
                except Exception:
                    selection = {s: True for s in SUITES} | {"full": True}
            else:
                selection = {s: True for s in SUITES} | {"full": True}
        elif args.files is not None:
            selection = select(args.files)
        else:
            selection = {s: True for s in SUITES} | {"full": True}

        lines = [f"{s}={'true' if selection[s] else 'false'}" for s in SUITES]
        lines.append(f"full={'true' if selection['full'] else 'false'}")
        output_str = "\n".join(lines) + "\n"

        sys.stdout.write(output_str)

        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as f:
                f.write(output_str)
        return 0

    if args.command == "gate":
        try:
            needs_obj = json.loads(args.needs)
        except Exception:
            sys.stderr.write("ci-gate: bad needs json\n")
            return 1

        problems = gate(needs_obj)
        if problems:
            for prob in problems:
                sys.stderr.write(f"{prob}\n")
            return 1

        sys.stdout.write("ci-gate: ok\n")
        return 0

    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
