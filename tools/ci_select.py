"""CI affected-suite selector and gate verdict."""

from __future__ import annotations

import argparse
from fnmatch import fnmatchcase
import json
import os
import subprocess
import sys

#: Jobs that run on every change: never selected, always required by the gate.
ALWAYS: tuple[str, ...] = ("scrub", "file-size", "new-code-lang", "changelog-docs")

SUITES: tuple[str, ...] = (
    "pytest",
    "pytest-postgres",
    "rust",
    "rust-worker",
    "rust-spike",
    "browser",
)

KNOWN_PREFIXES: tuple[str, ...] = (
    "src/",
    "tests/",
    "tools/",
    "vendor/",
    "rust/",
)

# The standalone Temporal worker job also runs the probe admission guard tests.
RUST_WORKER_INPUTS: tuple[str, ...] = ("tools/probe_admission_policy_guard.py",)

# D-5294: Rust tests also compile/read repository inputs outside rust/.
# Keep these before the docs-only shortcut; tests/test_ci_select_rust_inputs.py
# scans Rust sources and rejects repository reads missing from this list.
RUST_INPUTS: tuple[str, ...] = (
    "pyproject.toml",  # pg-mainff verifies the keeper console-entry inventory.
    "src/plangraph/catalog_data.py",
    "src/plangraph/routing_errors.py",
    "deploy/feeder/**",
    "deploy/systemd/pg-feeder.service",
    "src/plangraph/keeper/errors.py",
    "src/plangraph/planner/routing.py",
    "src/plangraph/planner/measured_down.py",
    # D-6054: native/agy quota isolation regressions exercise these boundaries.
    "src/plangraph/planner/provider_stop.py",
    "src/plangraph/planner/measured_down.py",
    "src/plangraph/routing_support/liveness.py",
    # D-6054: rendered OpenCode catalog integration-test inputs.
    "src/plangraph/planner/legs.py",
    "src/plangraph/routing_support/__init__.py",
    "src/plangraph/routing_support/catalog_schema.sql",
    "src/plangraph/routing_support/catalog_benchmark_seed.sql",
    "src/plangraph/routing_support/opencode_overlay.py",
    "src/plangraph/routing_support/opencode_render.py",
    "src/plangraph/routing_support/resolver.py",
    "configuration/opencode/providers.json",
    # rust/pg-spawnd/src/round_progress_tests.rs reads the max_rounds setting (one stop rule).
    "src/plangraph/settings.py",
    ".github/workflows/**",
    # rust/pg-spawnd/tests/ci_step_timeout.rs reads the bounded-step run script.
    ".github/actions/bounded-step/action.yml",
    "rust-toolchain.toml",
    ".docs-map",
    ".gitignore",
    "docs/schemas/**",
    "docs/flow-watchdog.md",
    "docs/placement-rules.md",
    "docs/rust-hooks.md",
    "docs/spawner-reliability.md",
    "docs/workspace-resolver.md",
    "changelog.d/FLEET-WINDOW-CATALOG.md",
    "changelog.d/PLACEMENT-RULES-AUDIT.md",
    "changelog.d/SPAWNER-RL12-B1.md",
    "deploy/systemd/plangraph-agents-view.service",
    "deploy/systemd/plangraph-agents-view.timer",
    "docs/setup.md",
    "setup/manifest.toml",
    "setup/placement-rules.schema.toml",
    "src/plangraph/web/agents_schema.json",
    "src/plangraph/config/prompt_transport_v1.json",
    # rust/plangraph-cli/src/runner/config.rs tests parse the example runner config.
    "docs/operations/ci-runners.example.toml",
    "src/plangraph/spawner/**",
    "src/plangraph/keeper/__init__.py",
    "src/plangraph/keeper/auth.py",
    "src/plangraph/keeper/context_sources.py",
    "src/plangraph/keeper/db.py",
    "src/plangraph/keeper/evidence.py",
    "src/plangraph/keeper/evidence_decisions.py",
    "src/plangraph/keeper/evidence_types.py",
    "src/plangraph/keeper/store.py",
    "src/plangraph/keeper/evidence_spawner.py",
    "src/plangraph/routing_support/leg_state.py",
    "tests/fixtures/doctor_sources.json",
    "src/plangraph/config/role_headers.json",
    "tests/fixtures/keeper_mcp_replies.json",
    "tests/fixtures/keeper_mcp_tools.json",
    "tests/fixtures/redact_parity.json",
    "tests/fixtures/writer_docs_map.json",
    "tests/hooks/parity_payloads.json",
    "tools/changelog_fold.py",
    "tools/check_changelog_docs.py",
    "tools/check_file_size.py",
    "tools/check_new_code_lang.py",
    "tools/ci_select.py",
    "tools/claude_usage_policy.json",
    "tools/ci_commit_gate.py",
    "tools/check_push_gate.py",
    "tools/merge_batch_util.py",
    "tools/gate_wiring_check.py",
    "tools/split/**",
    "tools/hooks/pre-push",
    "tools/merge_batch.py",
    "tools/merge_batch_land.py",
    "tools/dell_ci_jobs.py",
    "tools/new_code_allowlist.txt",
    *RUST_WORKER_INPUTS,
    "tools/seat_verdict.py",
    "tools/preflight.py",
    "tools/preflight_private_scrub.py",
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
        or basename == ".docs-map"
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

    # 5. Otherwise (no full trigger) select by path (ALWAYS jobs need no selection)
    res = {s: False for s in SUITES}
    res["full"] = False

    for p in norm_paths:
        if p.startswith(("src/plangraph/web/", "tests/browser/", "vendor/")) or p in ("tests/conftest.py", "pyproject.toml", "src/plangraph/render.py"):
            res["browser"] = True
        if any(fnmatchcase(p, pattern) for pattern in RUST_INPUTS):
            res["rust"] = True
        if p in RUST_WORKER_INPUTS:
            res["rust-worker"] = True
        if p.startswith("skills/") and p.endswith(".md"):
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
            if (p.startswith(("rust/worker/", "rust/worker-core/", "rust/pg-spawnd/"))
                    or fnmatchcase(p, "rust/Cargo.*")):
                res["rust-worker"] = True
            if (
                (not p.startswith("rust/spikes/") and not p.startswith("rust/worker/"))
                or p.startswith("rust/worker-core/")
            ):
                res["rust"] = True

    return res


#: Label a router GO row puts on a pull request head to name it the merge candidate (D-6392 run-once order).
FULL_LABEL = "ci-full"
BATCH_PREFIX = "batch/"


def full_run_required(event: str, base_ref: str = "", head_ref: str = "", labels=()) -> bool:
    """The full suite runs once per merge candidate; every other pull request gets the affected selection.

    A pull_request event gets the full suite only for a batch/* head or a head carrying FULL_LABEL, and only
    when it targets main (a draft or ready lane pull request, or one stacked on another branch, is a selection).
    Push, schedule, workflow_dispatch and merge_group events are the record or the candidate itself: full."""
    if event != "pull_request":
        return True
    if base_ref != "main":
        return False
    return head_ref.startswith(BATCH_PREFIX) or FULL_LABEL in set(labels or ())


def gate(needs: dict, require_full: bool = False) -> list[str]:
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
    if require_full and outputs.get("full") != "true":
        problems.append("select: merge candidate ran the affected selection, not the full suite")
    for suite in ALWAYS + SUITES + ("rust-windows",):
        selection = "rust" if suite == "rust-windows" else suite
        selected = outputs.get("full") == "true" or suite in ALWAYS or outputs.get(selection) != "false"
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
    for sub in (select_parser, gate_parser):
        sub.add_argument("--event", default=None, help="GitHub event name; picks full or affected selection")
        sub.add_argument("--base-ref", default="", help="Pull request base branch name")
        sub.add_argument("--head-ref", default="", help="Pull request head branch name")
        sub.add_argument("--labels", default="[]", help="JSON list of the pull request label names")

    gate_parser.add_argument("--needs", type=str, required=True, help="JSON string of needs object")

    args = parser.parse_args(argv)

    candidate = False
    if args.command in ("select", "gate") and args.event is not None:
        try:
            labels = json.loads(args.labels) if args.labels else []
            if not isinstance(labels, list):
                raise ValueError
        except ValueError:
            sys.stderr.write("ci_select: --labels must be a JSON list\n")
            return 1
        candidate = full_run_required(args.event, args.base_ref, args.head_ref, labels)

    if args.command == "select":
        if args.full or candidate:
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

        problems = gate(needs_obj, require_full=candidate)
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
